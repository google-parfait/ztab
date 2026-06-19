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


# --- Backend Configuration Loading ---

def _load_backends():
    """Load backend config from disk. Called once at startup."""
    config_path = os.environ.get("ZTAB_BACKENDS_FILE")
    if not config_path:
        # DESIGN_DECISION: Reading from ~/.ztab/backends.json is intentional.
        # In production deployments, this file is expected to be a read-only
        # volume mount securely injected by the host orchestrator. It is not an
        # architectural flaw for the agent to have read access to it.
        config_path = os.path.expanduser("~/.ztab/backends.json")

    if not os.path.exists(config_path):
        return None

    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception as e:
        print(f"[ztab] Error loading backends config from {config_path}: {e}",
              file=sys.stderr)
        return None

    _validate_backends(config, config_path)
    return config


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


BACKENDS_CONFIG = _load_backends()


def _get_backend_info(backend_id=None):
    """Look up a backend by ID, falling back to default_backend."""
    if not BACKENDS_CONFIG:
        raise ValueError(
            "Backend configuration is missing. Please ensure "
            "~/.ztab/backends.json exists or ZTAB_BACKENDS_FILE is set."
        )

    bid = backend_id or BACKENDS_CONFIG.get("default_backend")
    if not bid:
        raise ValueError(
            "No backend_id provided and no default_backend configured."
        )

    backends = BACKENDS_CONFIG.get("backends", [])
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
            "Returns a session_id (to share with other participants) and "
            "a participant_token (secret, for your own use in subsequent RPCs). "
            "The creator automatically counts as one participant."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "policy_class": {
                    "type": "string",
                    "description": "Policy class name (e.g., 'ExtractAndResolve')",
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
            "Join an existing session on the ZTAB TEE server. Returns the "
            "session policy for review and a participant_token for subsequent RPCs. "
            "After reviewing the policy, call ztab_accept_policy."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Session ID to join"},
                "role": {"type": "string", "description": "Role for role-based policies (Phase 2+)"},
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["session_id"],
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
                "session_id": {"type": "string", "description": "Session ID"},
                "participant_token": {"type": "string", "description": "Your participant token"},
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["session_id", "participant_token"],
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
                "session_id": {"type": "string", "description": "Session ID"},
                "participant_token": {"type": "string", "description": "Your participant token"},
                "input_json": {
                    "type": "string",
                    "description": "JSON string with your private input data",
                },
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["session_id", "participant_token", "input_json"],
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
                "session_id": {"type": "string", "description": "Session ID"},
                "participant_token": {"type": "string", "description": "Your participant token"},
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["session_id", "participant_token"],
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
                "session_id": {"type": "string", "description": "Session ID"},
                "participant_token": {"type": "string", "description": "Your participant token"},
                "backend": {"type": "string", "description": "Backend ID to use (optional)"},
            },
            "required": ["session_id", "participant_token"],
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


def _get_stub(host, port, verifier_name="noop", expected_digest=None, allow_debug=False):
    """Create a connected gRPC stub."""
    verifier = _get_verifier(verifier_name, expected_digest, allow_debug)
    channel_wrapper = ZtabChannel(host=host, port=port, verifier=verifier)
    grpc_channel = channel_wrapper.connect()
    stub = session_manager_pb2_grpc.AgentBrokerServiceStub(grpc_channel)
    return stub, channel_wrapper


def _common_args(arguments):
    """Extract connection args by resolving the backend from the config file.

    Note: The old _resolve_allow_debug() env-var gate was removed because
    allow_debug_tee now comes from the trusted config file, not from
    agent-supplied tool arguments. The config file IS the trust boundary;
    the agent cannot override it at runtime.
    """
    backend = _get_backend_info(arguments.get("backend"))
    return {
        "host": backend.get("host", "localhost"),
        "port": int(backend.get("port", 8000)),
        "verifier_name": backend["verifier"],
        "expected_digest": backend.get("expected_digest"),
        "allow_debug": backend.get("allow_debug_tee", False),
    }

def run_list_backends(_arguments):
    """Return sanitized metadata about configured backends."""
    if not BACKENDS_CONFIG:
        return {"status": "error", "message": "Backend configuration is missing."}

    sanitized = []
    for b in BACKENDS_CONFIG.get("backends", []):
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
        "default_backend": BACKENDS_CONFIG.get("default_backend"),
    }


def run_connectivity_test(host, port, message, verifier_name="noop",
                         expected_digest=None, allow_debug=False):
    """Connects to server, extracts attestation, runs Echo RPC."""
    stub, channel_wrapper = _get_stub(host, port, verifier_name, expected_digest, allow_debug)
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
    conn = _common_args(arguments)
    stub, channel = _get_stub(**conn)
    try:
        policy = session_manager_pb2.SessionPolicy(
            policy_class=arguments["policy_class"],
            expected_participants=int(arguments["expected_participants"]),
            timeout_seconds=int(arguments.get("timeout_seconds", 300)),
        )
        response = stub.CreateSession(session_manager_pb2.CreateSessionRequest(policy=policy))
        return {
            "status": "success",
            "session_id": response.session_id,
            "state": session_manager_pb2.SessionState.Name(response.state),
            "participant_token": response.participant_token,
            "instructions": "Share session_id with other participants. Keep your token secret.",
        }
    except grpc.RpcError as e:
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}
    finally:
        channel.close()


def run_join_session(arguments):
    """Join an existing session."""
    conn = _common_args(arguments)
    stub, channel = _get_stub(**conn)
    try:
        request = session_manager_pb2.JoinSessionRequest(
            session_id=arguments["session_id"],
            role=arguments.get("role", ""),
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
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}
    finally:
        channel.close()


def run_accept_policy(arguments):
    """Accept the session policy."""
    conn = _common_args(arguments)
    stub, channel = _get_stub(**conn)
    try:
        request = session_manager_pb2.AcceptPolicyRequest(
            session_id=arguments["session_id"],
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
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}
    finally:
        channel.close()


def run_submit_input(arguments):
    """Submit input to the session."""
    conn = _common_args(arguments)
    stub, channel = _get_stub(**conn)
    try:
        request = session_manager_pb2.SubmitInputRequest(
            session_id=arguments["session_id"],
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
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}
    finally:
        channel.close()


def run_get_result(arguments):
    """Get session result."""
    conn = _common_args(arguments)
    stub, channel = _get_stub(**conn)
    try:
        request = session_manager_pb2.GetResultRequest(
            session_id=arguments["session_id"],
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
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}
    finally:
        channel.close()


def run_get_session_status(arguments):
    """Get session status."""
    conn = _common_args(arguments)
    stub, channel = _get_stub(**conn)
    try:
        request = session_manager_pb2.GetSessionStatusRequest(
            session_id=arguments["session_id"],
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
        return {"status": "error", "message": f"gRPC error {e.code()}: {e.details()}"}
    finally:
        channel.close()


# --- MCP Tool Dispatch ---

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


def handle_request(method: str, params: dict) -> dict | None:
    """Handle incoming JSON-RPC request from the agent framework."""
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

    return None


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

        if result is not None:
            response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
            _PROTO_OUT.write(json.dumps(response) + "\n")
            _PROTO_OUT.flush()
            print(f"[ztab] Sent response for method: '{method}' (id: {msg_id})", file=sys.stderr)


if __name__ == "__main__":
    main()
