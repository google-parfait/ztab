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

"""ZTAB MCP Smoke Test.

Mechanically validates the MCP server by launching it as a subprocess and
sending JSON-RPC requests over stdio. No LLM or agent framework required.

Prerequisites:
  - Python dependencies installed (grpcio, cryptography).
  - Proto stubs compiled in agent/pb2/.
  - ZTAB TEE server running (default: localhost:8000).

Usage:
    python3 smoke_test.py [--host HOST] [--port PORT] [--python PYTHON_PATH]

Exit codes:
    0 = all checks passed
    1 = one or more checks failed
"""

import argparse
import json
import subprocess
import sys
import os
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MCP_SERVER = os.path.join(SCRIPT_DIR, "mcp_server.py")

# ANSI colors for terminal output.
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


def send_rpc(proc, method, params=None, msg_id=1):
    """Send a JSON-RPC request and read the response."""
    request = {
        "jsonrpc": "2.0",
        "id": msg_id,
        "method": method,
    }
    if params is not None:
        request["params"] = params

    line = json.dumps(request) + "\n"
    proc.stdin.write(line)
    proc.stdin.flush()

    # Read one line of response.
    response_line = proc.stdout.readline()
    if not response_line:
        return None
    return json.loads(response_line.strip())


def check(label, condition, detail=""):
    """Print a check result and return True/False."""
    if condition:
        print(f"  {GREEN}✓{RESET} {label}")
        return True
    else:
        msg = f"  {RED}✗{RESET} {label}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        return False


def main():
    parser = argparse.ArgumentParser(description="ZTAB MCP smoke test")
    parser.add_argument("--host", default="localhost", help="ZTAB server host")
    parser.add_argument("--port", type=int, default=8000, help="ZTAB server port")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python interpreter to use for the MCP server",
    )
    args = parser.parse_args()

    print(f"\n{YELLOW}ZTAB MCP Smoke Test{RESET}")
    print(f"  Server target: {args.host}:{args.port}")
    print(f"  Python: {args.python}")
    print(f"  MCP server: {MCP_SERVER}")
    print()

    # Start the MCP server as a subprocess.
    print("Starting MCP server subprocess...")
    proc = subprocess.Popen(
        [args.python, MCP_SERVER],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=SCRIPT_DIR,
    )

    passed = 0
    failed = 0

    try:
        # --- Test 1: Initialize ---
        print(f"\n{YELLOW}Test 1: MCP Initialize{RESET}")
        resp = send_rpc(proc, "initialize", {}, msg_id=1)
        if check("Response received", resp is not None):
            result = resp.get("result", {})
            check("Protocol version present", "protocolVersion" in result)
            check(
                "Server name is 'ztab'",
                result.get("serverInfo", {}).get("name") == "ztab",
                f"got: {result.get('serverInfo', {}).get('name')}",
            )
            ok = all([
                resp is not None,
                "protocolVersion" in result,
                result.get("serverInfo", {}).get("name") == "ztab",
            ])
        else:
            ok = False
        passed += ok
        failed += not ok

        # Send initialized notification (no response expected).
        notify = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        proc.stdin.write(json.dumps(notify) + "\n")
        proc.stdin.flush()

        # --- Test 2: Tools List ---
        print(f"\n{YELLOW}Test 2: Tools List{RESET}")
        resp = send_rpc(proc, "tools/list", {}, msg_id=2)
        if check("Response received", resp is not None):
            tools = resp.get("result", {}).get("tools", [])
            tool_names = [t["name"] for t in tools]
            check("At least one tool listed", len(tools) > 0, f"got {len(tools)}")
            check(
                "'ztab_test_connection' in tool list",
                "ztab_test_connection" in tool_names,
                f"tools: {tool_names}",
            )
            ok = len(tools) > 0 and "ztab_test_connection" in tool_names
        else:
            ok = False
        passed += ok
        failed += not ok

        # --- Test 3: Connectivity Tool Call ---
        print(f"\n{YELLOW}Test 3: ztab_test_connection{RESET}")
        resp = send_rpc(
            proc,
            "tools/call",
            {
                "name": "ztab_test_connection",
                "arguments": {
                    "host": args.host,
                    "port": args.port,
                    "message": "smoke-test",
                },
            },
            msg_id=3,
        )
        if check("Response received", resp is not None):
            content = resp.get("result", {}).get("content", [])
            if check("Content array not empty", len(content) > 0):
                payload = json.loads(content[0].get("text", "{}"))
                check(
                    "Status is 'success'",
                    payload.get("status") == "success",
                    f"got: {payload.get('status')}: {payload.get('message', '')}",
                )
                check(
                    "Attestation token present",
                    payload.get("attestation_token_present") is True,
                )
                check(
                    "Echo response contains test message",
                    "smoke-test" in payload.get("echo_response", ""),
                    f"got: {payload.get('echo_response')}",
                )
                claims = payload.get("attestation_claims", {})
                check(
                    "JWT issuer is confidentialcomputing.googleapis.com",
                    "confidentialcomputing" in claims.get("iss", ""),
                    f"got: {claims.get('iss')}",
                )
                ok = (
                    payload.get("status") == "success"
                    and payload.get("attestation_token_present") is True
                    and "smoke-test" in payload.get("echo_response", "")
                )
            else:
                ok = False
        else:
            ok = False
        passed += ok
        failed += not ok

    finally:
        proc.terminate()
        proc.wait(timeout=5)

    # --- Summary ---
    print(f"\n{'=' * 50}")
    total = passed + failed
    if failed == 0:
        print(f"{GREEN}All {total} tests passed.{RESET}")
    else:
        print(f"{RED}{failed}/{total} tests FAILED.{RESET}")
    print()

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
