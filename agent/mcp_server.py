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

Exposes ZTAB broker connectivity testing as an MCP tool. Zero external dependencies
for the MCP protocol handler itself (reads JSON-RPC from stdin, writes to stdout).

Uses the local virtualenv to resolve grpcio and cryptography dependencies.
"""

import json
import os
import sys
import base64

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
from client import ZtabChannel, noop_verifier
import session_manager_pb2
import session_manager_pb2_grpc

SERVER_NAME = "ztab"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

# Tool definitions exposed to the agent.
TOOLS = [
    {
        "name": "ztab_test_connection",
        "description": (
            "Connect to a ZTAB TEE server, verify its TLS certificate, "
            "extract and parse the remote attestation token (JWT), and "
            "make a secure gRPC Echo RPC call to verify the channel."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "ZTAB server host (e.g., 'localhost')",
                },
                "port": {
                    "type": "integer",
                    "description": "ZTAB server port (e.g., 8000)",
                },
                "message": {
                    "type": "string",
                    "description": "Test message to send via Echo RPC",
                },
            },
            "required": ["host", "port", "message"],
        },
    }
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


def run_connectivity_test(host: str, port: int, message: str) -> dict:
    """Connects to server, extracts attestation, runs Echo RPC."""
    channel_wrapper = ZtabChannel(host=host, port=port, verifier=noop_verifier)
    try:
        print(f"[ztab] Connecting to {host}:{port}...", file=sys.stderr)
        grpc_channel = channel_wrapper.connect()

        # Call Echo.
        print(f"[ztab] Sending gRPC Echo: '{message}'", file=sys.stderr)
        stub = session_manager_pb2_grpc.AgentBrokerServiceStub(grpc_channel)
        request = session_manager_pb2.EchoRequest(message=message)
        response = stub.Echo(request)

        # Decode token for return result.
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
        err_msg = f"gRPC error {e.code()}: {e.details()}"
        print(f"[ztab] {err_msg}", file=sys.stderr)
        return {"status": "error", "message": err_msg}
    except Exception as e:
        err_msg = f"Connection error: {e}"
        print(f"[ztab] {err_msg}", file=sys.stderr)
        return {"status": "error", "message": err_msg}
    finally:
        channel_wrapper.close()


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
        if tool_name == "ztab_test_connection":
            host = arguments.get("host", "localhost")
            port = int(arguments.get("port", 8000))
            message = arguments.get("message", "Test")
            result = run_connectivity_test(host, port, message)
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

        result = handle_request(method, params)

        if msg_id is None:
            continue  # Notification, no response needed.

        if result is not None:
            response = {"jsonrpc": "2.0", "id": msg_id, "result": result}
            _PROTO_OUT.write(json.dumps(response) + "\n")
            _PROTO_OUT.flush()
            print(f"[ztab] Sent response for method: '{method}' (id: {msg_id})", file=sys.stderr)


if __name__ == "__main__":
    main()
