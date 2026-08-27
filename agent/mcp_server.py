#!/usr/bin/env python3
# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""ZTAB Stdio MCP Server.

Exposes ZTAB broker RPCs as MCP tools. Zero external dependencies
for the MCP protocol handler itself (reads JSON-RPC from stdin, writes to stdout).

Uses the local virtualenv to resolve grpcio and cryptography dependencies.
"""

import base64
import json
import os
import sys
import uuid

# CRITICAL: Save the real stdout for JSON-RPC protocol output, then redirect
# sys.stdout to stderr. This prevents print() calls in client.py and grpcio
# from corrupting the MCP protocol stream.
_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr

# Add the directory containing this script and its pb2/ subdirectory to sys.path
# so we can import client.py and the compiled proto stubs.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
sys.path.insert(0, os.path.join(SCRIPT_DIR, "pb2"))

import grpc
from client import ZtabChannel
import session_manager_pb2
import session_manager_pb2_grpc

SERVER_NAME = "ztab"
SERVER_VERSION = "0.2.0"
PROTOCOL_VERSION = "2024-11-05"

# Sentinel for handle_request: distinguishes "unknown method"
# from "notification with no response" (which returns None).
_NOT_HANDLED = object()


# --- Backend Configuration Loading ---

def _load_backends_from(config_path):
    """Load and validate backend config from a specific file path."""
    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception as e:
        print(f"[ztab] Error loading backends config from {config_path}: {e}",
              file=sys.stderr)
        return None

    _validate_backends(config, config_path)
    return config


def _backends_config_path():
    """Return the resolved path to backends.json."""
    path = os.environ.get("ZTAB_BACKENDS_FILE")
    if not path:
        # DESIGN_DECISION: Reading from ~/.ztab/backends.json is intentional.
        # In production deployments, this file is expected to be a read-only
        # volume mount securely injected by the host orchestrator. It is not an
        # architectural flaw for the agent to have read access to it.
        path = os.path.expanduser("~/.ztab/backends.json")
    return path


def _validate_backends(config, config_path):
    """Validate backends config schema at startup. Logs warnings to stderr."""
    if not isinstance(config.get("backends"), list):
        print(f"[ztab] WARNING: {config_path}: 'backends' must be a list",
              file=sys.stderr)
        return

    seen_ids = set()
    required_fields = ["backend_id", "host", "port", "verifier"]
    for i, b in enumerate(config["backends"]):
        for field in required_fields:
            if field not in b:
                raise ValueError(f"Backend '{b.get('backend_id', i)}' missing required field '{field}'")
        bid = b.get("backend_id")
        if bid and bid in seen_ids:
            print(f"[ztab] WARNING: {config_path}: duplicate backend_id "
                  f"'{bid}'", file=sys.stderr)
        if bid:
            seen_ids.add(bid)

    default = config.get("default_backend")
    if default and default not in seen_ids:
        print(f"[ztab] WARNING: {config_path}: default_backend '{default}' "
              f"does not match any backend_id", file=sys.stderr)


# --- Lazy Backend Loading with mtime Check ---
# Instead of loading once at startup, we check the file's mtime on each access.
# If it changed, we reload from disk and flush _CHANNEL_CACHE so stale gRPC
# connections to old host:port are discarded. This enables the agent to write
# new backends to backends.json after the MCP server is already running.
_backends_mtime_ns = 0
_backends_config = None


def _get_backends():
    """Return the current backends config, reloading if the file changed."""
    global _backends_mtime_ns, _backends_config
    path = _backends_config_path()
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    if st.st_mtime_ns != _backends_mtime_ns:
        new_config = _load_backends_from(path)
        if _backends_config is not None and new_config is not None:
            # Config changed after initial load — flush stale channels
            print(f"[ztab] backends.json changed, reloading and flushing channel cache",
                  file=sys.stderr)
            for channel, _ in _CHANNEL_CACHE.values():
                try:
                    channel.close()
                except Exception:
                    pass
            _CHANNEL_CACHE.clear()
        _backends_config = new_config
        _backends_mtime_ns = st.st_mtime_ns
    return _backends_config


def _get_backend_info(backend_id=None):
    """Look up a backend by ID, falling back to default_backend."""
    config = _get_backends()
    if not config:
        raise ValueError(
            "Backend configuration is missing. Please ensure "
            "~/.ztab/backends.json exists or ZTAB_BACKENDS_FILE is set."
        )

    bid = backend_id or config.get("default_backend")
    if not bid:
        raise ValueError(
            "No backend_id provided and no default_backend configured."
        )

    backends = config.get("backends", [])
    for b in backends:
        if b.get("backend_id") == bid:
            return b

    available = [b.get("backend_id") for b in backends]
    raise ValueError(
        f"Unknown backend '{bid}'. Available backends: {', '.join(available)}"
    )


# Tool definitions exposed to the agent.
TOOLS = [
    {
        "name": "ztab_list_backends",
        "description": "List the available TEE backends configured on this host.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "ztab_test_connection",
        "description": (
            "Connect to a ZTAB TEE server, verify its TLS certificate, "
            "extract and parse the remote attestation token (JWT), and "
            "make a secure gRPC Echo RPC call to verify the channel. "
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "Test message for Echo RPC"},
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["message"],
        },
    },
    {
        "name": "ztab_create_session",
        "description": (
            "Create a new multi-agent session on the ZTAB TEE server. "
            "Returns an invitation_token (to share with other participants) and "
            "a participant_token (secret, for your own use in subsequent RPCs). "
            "The creator automatically counts as one participant."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "policy_class": {
                    "type": "string",
                    "description": "Policy class name (e.g., 'ScheduleOverlap')",
                },
                "expected_participants": {
                    "type": "integer",
                    "description": "Total number of participants (including creator). Must be >= 2.",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Per-state timeout in seconds (default: 300)",
                },
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["policy_class", "expected_participants"],
        },
    },
    {
        "name": "ztab_join_session",
        "description": (
            "Join an existing session on the ZTAB TEE server using an "
            "invitation_token received from the session creator. Returns the "
            "session policy for review and a participant_token for subsequent RPCs. "
            "After reviewing the policy, call ztab_accept_policy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "invitation_token": {"type": "string", "description": "Invitation token received from session creator"},
                "role": {"type": "string", "description": "Role for role-based policies (Phase 2+)"},
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["invitation_token"],
        },
    },
    {
        "name": "ztab_accept_policy",
        "description": (
            "Accept the policy of a session you've joined. Must be called "
            "after join_session and before submit_input. When all participants "
            "accept, the session transitions to SEALED."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "participant_token": {"type": "string", "description": "Your participant token"},
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["participant_token"],
        },
    },
    {
        "name": "ztab_submit_input",
        "description": (
            "Submit your private input to a SEALED session. The input must be "
            "valid JSON conforming to the session's input schema. When all "
            "participants submit, the TEE processes the inputs via LLM."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "participant_token": {"type": "string", "description": "Your participant token"},
                "input_json": {
                    "type": "string",
                    "description": "JSON string with your private input data",
                },
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["participant_token", "input_json"],
        },
    },
    {
        "name": "ztab_get_result",
        "description": (
            "Get the result of a completed session. Returns the LLM-generated "
            "result if the session is CLOSED, error details if ABORTED, or "
            "current state if still in progress."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "participant_token": {"type": "string", "description": "Your participant token"},
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["participant_token"],
        },
    },
    {
        "name": "ztab_get_session_status",
        "description": (
            "Get the current status of a session: state, how many participants "
            "have joined, accepted, and submitted input."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "participant_token": {"type": "string", "description": "Your participant token"},
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["participant_token"],
        },
    },
]


def decode_jwt_payload(token: str) -> dict | None:
    """Decodes the payload of an unsigned JWT (alg=none) for display."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload_b64 = payload_b64.replace("-", "+").replace("_", "/")
    try:
        return json.loads(base64.b64decode(payload_b64))
    except Exception:
        return None


from verifier_factory import get_verifier as _get_verifier
from verifier_policy import (
    ItaPolicy,
    NoopPolicy,
    VerifierPolicy,
)


# --- Channel Cache ---
# Persist gRPC channels across tool calls so all RPCs for a given backend
# reuse the same TCP connection (HTTP/2 multiplexing). This eliminates the
# per-RPC TLS handshake and attestation overhead that caused the
# 329-connection storm.
_CHANNEL_CACHE = {}  # backend_id -> (ZtabChannel, stub)


def _build_policy_from_backend(
    backend: dict,
) -> VerifierPolicy:
    """Construct a VerifierPolicy from a backends.json entry."""
    verifier_name = backend.get("verifier", "")
    if verifier_name == "noop":
        return NoopPolicy()

    # Default to ITA.
    digest_val = backend.get(
        "expected_digest", ""
    )
    if isinstance(digest_val, list):
        digests = frozenset(digest_val)
    elif digest_val:
        digests = frozenset([digest_val])
    else:
        digests = frozenset()

    kwargs = {
        "expected_image_digests": digests,
        "allow_debug": backend.get(
            "allow_debug_tee", False
        ),
        "allow_memory_monitoring": backend.get(
            "allow_memory_monitoring", False
        ),
        "expected_project_id": backend.get(
            "expected_project_id", ""
        ),
        "expected_service_account": backend.get(
            "expected_service_account", ""
        ),
    }

    digest = backend.get("expected_image_digest")
    if digest:
        kwargs["expected_image_digests"] = frozenset([digest])

    min_cs = backend.get("min_cs_version")
    if min_cs not in (None, ""):
        kwargs["min_cs_version"] = int(min_cs)
    return ItaPolicy(**kwargs)


def _get_stub(host, port, policy):
    """Create a connected gRPC stub (uncached — use _get_cached_stub instead)."""
    verifier = _get_verifier(policy)
    channel_wrapper = ZtabChannel(
        host=host,
        port=port,
        verifier=verifier,
    )
    grpc_channel = channel_wrapper.connect()
    stub = session_manager_pb2_grpc.AgentBrokerServiceStub(grpc_channel)
    return channel_wrapper, stub


def _get_cached_stub(arguments):
    """Return a cached (channel, stub, backend_info) for the resolved backend.

    Creates a new connection on first call for each backend_id, then reuses
    it for all subsequent calls. If a call fails with UNAVAILABLE (stale
    channel), the caller should call _evict_backend() and retry.
    """
    backend = _get_backend_info(arguments.get("backend"))
    bid = backend["backend_id"]

    if bid not in _CHANNEL_CACHE:
        policy = _build_policy_from_backend(backend)
        channel, stub = _get_stub(
            host=backend.get("host", "localhost"),
            port=int(backend.get("port", 8000)),
            policy=policy,
        )
        _CHANNEL_CACHE[bid] = (channel, stub)
        print(f"[ztab] Channel created for backend '{bid}'", file=sys.stderr)

    channel, stub = _CHANNEL_CACHE[bid]
    return channel, stub, backend


def _evict_backend(arguments):
    """Evict a stale channel from the cache (e.g., after UNAVAILABLE)."""
    backend = _get_backend_info(arguments.get("backend"))
    bid = backend["backend_id"]
    entry = _CHANNEL_CACHE.pop(bid, None)
    if entry:
        channel, _ = entry
        try:
            channel.close()
        except Exception:
            pass
        print(f"[ztab] Evicted stale channel for backend '{bid}'", file=sys.stderr)


def _common_args(arguments):
    """Extract connection args by resolving the backend from the config file.

    Returns a dict with 'host', 'port', and 'policy'
    (a VerifierPolicy instance).
    """
    backend = _get_backend_info(
        arguments.get("backend")
    )
    return {
        "host": backend.get("host", "localhost"),
        "port": int(backend.get("port", 8000)),
        "policy": _build_policy_from_backend(
            backend
        ),
    }

def run_list_backends(_arguments):
    """Return sanitized metadata about configured backends."""
    config = _get_backends()
    if not config:
        return {"status": "error", "message": "Backend configuration is missing."}

    sanitized = []
    for b in config.get("backends", []):
        sec_level = "production" if b.get("verifier") != "noop" else "development"
        sanitized.append({
            "backend_id": b.get("backend_id"),
            "name": b.get("name"),
            "description": b.get("description"),
            "security_level": sec_level,
        })

    return {
        "status": "success",
        "backends": sanitized,
        "default_backend": config.get("default_backend"),
    }


def run_connectivity_test(
    host, port, message, policy,
):
    """Connects to server, extracts attestation, runs Echo RPC.

    Note: This function intentionally does NOT use the channel cache because
    it is a diagnostic tool — the user expects it to perform a fresh TLS
    handshake and attestation every time it is called.
    """
    channel_wrapper, stub = _get_stub(host, port, policy)
    try:
        request = session_manager_pb2.EchoRequest(message=message)
        response = stub.Echo(request)
        token = channel_wrapper.attestation_token
        claims = decode_jwt_payload(token) if token else None
        return {
            "status": "success",
            "message": "ZTAB connectivity test passed.",
            "tls_handshake": "OK (CN=ztab-tee)",
            "attestation_token_present": token is not None,
            "attestation_claims": claims,
            "echo_response": response.message,
        }
    except grpc.RpcError as e:
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}
    except Exception as e:
        return {"status": "error", "message": f"Connection error: {e}"}
    finally:
        channel_wrapper.close()


def run_create_session(arguments):
    """Create a new session."""
    channel, stub, backend_info = _get_cached_stub(arguments)
    try:
        policy = session_manager_pb2.SessionPolicy(
            policy_class=arguments["policy_class"],
            expected_participants=int(
                arguments["expected_participants"]
            ),
            timeout_seconds=int(
                arguments.get("timeout_seconds", 300)
            ),
        )
        metadata = []
        creator_token = backend_info.get(
            'creator_token', ''
        )
        if creator_token:
            metadata.append(
                ('x-ztab-creator-token', creator_token)
            )
        request = session_manager_pb2.CreateSessionRequest(
            policy=policy,
            # Auto-generate nonce for idempotent retry safety.
            # Agents don't need to know about this.
            client_nonce=arguments.get(
                "client_nonce", str(uuid.uuid4())
            ),
        )
        response = stub.CreateSession(
            request, metadata=metadata or None
        )
        return {
            "status": "success",
            "invitation_token": response.invitation_token,
            "state": session_manager_pb2.SessionState.Name(
                response.state
            ),
            "participant_token": response.participant_token,
            "instructions": (
                "Share invitation_token with other "
                "participants. Keep your "
                "participant_token secret."
            ),
        }
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNAVAILABLE:
            _evict_backend(arguments)
        return {
            "status": "error",
            "message": (
                f"gRPC error {e.code()}: {e.details()}"
            ),
        }


def run_join_session(arguments):
    """Join an existing session."""
    channel, stub, _ = _get_cached_stub(arguments)
    try:
        request = session_manager_pb2.JoinSessionRequest(
            invitation_token=arguments["invitation_token"],
            role=arguments.get("role", ""),
            # Auto-generate nonce for idempotent retry safety.
            client_nonce=arguments.get(
                "client_nonce", str(uuid.uuid4())
            ),
        )
        response = stub.JoinSession(request)
        return {
            "status": "success",
            "state": session_manager_pb2.SessionState.Name(response.state),
            "policy_class": response.policy.policy_class,
            "expected_participants": response.policy.expected_participants,
            "participant_token": response.participant_token,
            "instructions": "Review the policy, then call ztab_accept_policy.",
        }
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNAVAILABLE:
            _evict_backend(arguments)
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}


def run_accept_policy(arguments):
    """Accept the session policy."""
    channel, stub, _ = _get_cached_stub(arguments)
    try:
        request = session_manager_pb2.AcceptPolicyRequest(
            participant_token=arguments["participant_token"],
        )
        response = stub.AcceptPolicy(request)
        state_name = session_manager_pb2.SessionState.Name(response.state)
        result = {"status": "success", "state": state_name}
        if response.state == session_manager_pb2.SEALED:
            result["instructions"] = "All participants accepted. Submit your input now."
        else:
            result["instructions"] = "Waiting for other participants to accept."
        return result
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNAVAILABLE:
            _evict_backend(arguments)
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}


def run_submit_input(arguments):
    """Submit input to the session."""
    channel, stub, _ = _get_cached_stub(arguments)
    try:
        request = session_manager_pb2.SubmitInputRequest(
            participant_token=arguments["participant_token"],
            input_json=arguments["input_json"],
        )
        response = stub.SubmitInput(request)
        state_name = session_manager_pb2.SessionState.Name(response.state)
        result = {
            "status": "success",
            "state": state_name,
            "remaining_inputs": response.remaining_inputs,
        }
        if response.remaining_inputs == 0:
            result["instructions"] = "All inputs received. Call ztab_get_result."
        else:
            result["instructions"] = f"Waiting for {response.remaining_inputs} more input(s)."
        return result
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNAVAILABLE:
            _evict_backend(arguments)
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}


def run_get_result(arguments):
    """Get session result."""
    channel, stub, _ = _get_cached_stub(arguments)
    try:
        request = session_manager_pb2.GetResultRequest(
            participant_token=arguments["participant_token"],
        )
        response = stub.GetResult(request)
        state_name = session_manager_pb2.SessionState.Name(response.state)
        result = {"status": "success", "state": state_name}
        if response.state == session_manager_pb2.CLOSED:
            try:
                result["result"] = json.loads(response.result_json)
            except json.JSONDecodeError:
                result["result_raw"] = response.result_json
        elif response.state == session_manager_pb2.ABORTED:
            result["error_code"] = session_manager_pb2.SessionError.Name(response.error_code)
            result["error_detail"] = response.error_detail
        else:
            result["instructions"] = f"Result not yet available. State is {state_name}."
        return result
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNAVAILABLE:
            _evict_backend(arguments)
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}


def run_get_session_status(arguments):
    """Get session status."""
    channel, stub, _ = _get_cached_stub(arguments)
    try:
        request = session_manager_pb2.GetSessionStatusRequest(
            participant_token=arguments["participant_token"],
        )
        response = stub.GetSessionStatus(request)
        return {
            "status": "success",
            "state": session_manager_pb2.SessionState.Name(response.state),
            "participants_joined": response.participants_joined,
            "participants_accepted": response.participants_accepted,
            "inputs_received": response.inputs_received,
        }
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.UNAVAILABLE:
            _evict_backend(arguments)
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}


# --- MCP Tool Dispatch ---

# Build a schema lookup from the TOOLS list for argument validation.
_TOOL_SCHEMAS = {t["name"]: t.get("inputSchema", {}) for t in TOOLS}


def _validate_required_args(tool_name, arguments):
    """Validate that all required arguments are present before dispatch.

    Returns None if valid, or an error result dict if validation fails.
    """
    schema = _TOOL_SCHEMAS.get(tool_name, {})
    required = schema.get("required", [])
    missing = [r for r in required if r not in arguments]
    if missing:
        all_props = list(schema.get("properties", {}).keys())
        return {
            "status": "error",
            "message": (
                f"Missing required argument{'s' if len(missing) > 1 else ''}: "
                f"{', '.join(missing)}. "
                f"Required arguments for {tool_name}: {', '.join(required)}. "
                f"All accepted arguments: {', '.join(all_props)}."
            ),
        }
    return None


TOOL_HANDLERS = {
    "ztab_list_backends": run_list_backends,
    "ztab_test_connection": lambda args: run_connectivity_test(
        **{**_common_args(args), "message": args.get("message", "Test")}
    ),

    "ztab_create_session": run_create_session,
    "ztab_join_session": run_join_session,
    "ztab_accept_policy": run_accept_policy,
    "ztab_submit_input": run_submit_input,
    "ztab_get_result": run_get_result,
    "ztab_get_session_status": run_get_session_status,
}


def handle_request(method: str, params: dict):
    """Handle incoming JSON-RPC request from the agent framework.

    Returns a dict for successful responses, None for notifications
    that need no response, or _NOT_HANDLED for unknown methods.
    """
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {"tools": TOOLS}

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        handler = TOOL_HANDLERS.get(tool_name)
        if handler:
            # B3: Validate required args before dispatch for clear error messages.
            validation_error = _validate_required_args(tool_name, arguments)
            if validation_error:
                result = validation_error
            else:
                try:
                    result = handler(arguments)
                except ValueError as e:
                    result = {"status": "error", "message": str(e)}
                except Exception as e:
                    result = {"status": "error", "message": f"Tool execution failed: {e}"}
        else:
            result = {"status": "error", "message": f"Unknown tool: {tool_name}"}

        return {
            "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
        }

    return _NOT_HANDLED


def main():
    """Main stdio loop reading JSON-RPC from stdin, writing to stdout."""
    print("[ztab] MCP stdio server starting...", file=sys.stderr)

    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            print(f"[ztab] Invalid JSON format: {line[:100]}", file=sys.stderr)
            continue

        method = msg.get("method", "")
        params = msg.get("params", {})
        msg_id = msg.get("id")

        print(f"[ztab] Processing method: '{method}' (id: {msg_id})", file=sys.stderr)

        try:
            result = handle_request(method, params)
        except Exception as e:
            # Catch ALL exceptions to prevent the MCP server from crashing.
            # Return an error response to the LS so it can retry or report.
            print(f"[ztab] EXCEPTION in '{method}': {type(e).__name__}: {e}",
                  file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            if msg_id is not None:
                error_response = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {
                        "code": -32603,
                        "message": f"Internal error: {type(e).__name__}: {e}",
                    },
                }
                _PROTO_OUT.write(json.dumps(error_response) + "\n")
                _PROTO_OUT.flush()
            continue

        if msg_id is None:
            continue  # Notification, no response needed.

        if result is _NOT_HANDLED:
            # JSON-RPC 2.0 §5.1: unknown method → -32601 Method not found.
            # The Go MCP SDK (SEP-2575) sends server/discover as its first
            # probe. Without an error response it blocks until timeout and
            # kills the server process. Any error here triggers instant
            # fallback to legacy initialize.
            response = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {
                    "code": -32601,
                    "message": f"Method not found: {method}",
                },
            }
        else:
            response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        _PROTO_OUT.write(json.dumps(response) + "\n")
        _PROTO_OUT.flush()
        print(f"[ztab] Sent response for method: '{method}' (id: {msg_id})", file=sys.stderr)


if __name__ == "__main__":
    main()
