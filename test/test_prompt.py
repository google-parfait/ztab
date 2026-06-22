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

"""ZTAB Component Test: Direct LLM Prompt via Echo RPC.

Tests LLM prompt quality by formatting a scenario's prompt template with test
inputs and sending it to the TEE server's Echo endpoint for raw LLM inference.
No session management involved — purely for iterating on prompt quality.

This is a COMPONENT-LEVEL test. It tests the prompt template in isolation.
For the real end-to-end agent test, use test_cold_start.sh.
For the full session lifecycle test, use test_session.py.

Prerequisites:
  - Python dependencies installed (grpcio, cryptography).
  - Proto stubs compiled in agent/pb2/.
  - ZTAB TEE server running with a model loaded (not echo-only mode).

Usage:
    python3 -m test.test_prompt \\
        --scenario examples.calendar.scenario:CalendarScenario \\
        --host localhost --port 8000 \\
        [--allow_subset] [--test_case full_overlap]

Exit codes:
    0 = all checks passed
    1 = one or more checks failed
"""

import argparse
import importlib
import json
import logging
import os
import sys

# Setup python path.
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ZTAB_ROOT = os.path.dirname(SCRIPT_DIR)
AGENT_DIR = os.path.join(ZTAB_ROOT, "agent")
PB2_DIR = os.path.join(AGENT_DIR, "pb2")
sys.path.insert(0, ZTAB_ROOT)
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, PB2_DIR)

import grpc
from client import ZtabChannel
from verifier_factory import get_verifier
import session_manager_pb2 as pb2
import session_manager_pb2_grpc as pb2_grpc


def load_scenario(scenario_spec):
    """Load a scenario class from a module:ClassName spec string."""
    module_path, class_name = scenario_spec.rsplit(":", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls()


def run_prompt_test(scenario, test_case, args):
    """Format a prompt, send via Echo RPC, validate result.

    Returns:
        Tuple of (passed: bool, detail: str).
    """
    logging.info(f"--- Test case: {test_case.name} ---")

    # Format prompt using scenario's template + test case inputs.
    prompt = scenario.format_prompt(test_case.inputs)
    logging.info(f"Formatted prompt ({len(prompt)} bytes).")
    if args.show_prompt:
        print("=" * 60)
        print(prompt)
        print("=" * 60)

    # Send via Echo RPC.
    verifier = get_verifier(args.verifier, args.expected_digest,
                            args.allow_debug_tee)

    with ZtabChannel(args.host, args.port, verifier) as channel:
        stub = pb2_grpc.AgentBrokerServiceStub(channel.grpc_channel)
        logging.info("Sending prompt via Echo RPC...")
        try:
            echo_resp = stub.Echo(pb2.EchoRequest(message=prompt))
        except grpc.RpcError as e:
            return False, f"Echo RPC failed: {e.code()}: {e.details()}"

    raw_response = echo_resp.message
    logging.info(f"Echo response ({len(raw_response)} bytes).")
    if args.show_response:
        print("--- RAW RESPONSE ---")
        print(raw_response)
        print("--- END RESPONSE ---")

    # Try to extract JSON from the response.
    # The LLM may wrap its output in markdown code fences or add commentary.
    response_text = raw_response.strip()

    # Strip markdown code fences if present.
    if response_text.startswith("```"):
        lines = response_text.split("\n")
        # Remove first and last lines (the fences).
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        # Remove language tag if present (e.g., ```json).
        response_text = "\n".join(lines).strip()

    # Validate.
    allow = test_case.allow_subset or args.allow_subset
    return scenario.validate_result(response_text, test_case.expected_result,
                                    allow_subset=allow)


def main():
    parser = argparse.ArgumentParser(
        description="ZTAB Component Test: Direct LLM Prompt")
    parser.add_argument(
        "--scenario", required=True,
        help="Scenario spec in format 'module.path:ClassName'")
    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=8000, help="Server port")
    parser.add_argument(
        "--verifier", default="noop", choices=["noop", "ita"],
        help="Attestation verifier")
    parser.add_argument(
        "--expected_digest", default=None,
        help="Expected container image digest")
    parser.add_argument(
        "--allow_debug_tee", action="store_true", default=True,
        help="Allow debug TEE")
    parser.add_argument(
        "--allow_subset", action="store_true", default=False,
        help="Accept subset matches as passing")
    parser.add_argument(
        "--test_case", default=None,
        help="Run only the named test case (default: run all)")
    parser.add_argument(
        "--show_prompt", action="store_true", default=False,
        help="Print the formatted prompt before sending")
    parser.add_argument(
        "--show_response", action="store_true", default=False,
        help="Print the raw Echo response")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    scenario = load_scenario(args.scenario)
    logging.info(f"Loaded scenario: {scenario.name}")
    logging.info(f"Policy class: {scenario.policy_class}")

    test_cases = scenario.get_test_cases()
    if args.test_case:
        test_cases = [tc for tc in test_cases if tc.name == args.test_case]
        if not test_cases:
            logging.error(f"Test case '{args.test_case}' not found.")
            sys.exit(1)

    passed = 0
    failed = 0
    for tc in test_cases:
        ok, detail = run_prompt_test(scenario, tc, args)
        if ok:
            logging.info(f"PASS [{tc.name}]: {detail}")
            passed += 1
        else:
            logging.error(f"FAIL [{tc.name}]: {detail}")
            failed += 1

    logging.info(f"Results: {passed} passed, {failed} failed "
                 f"out of {len(test_cases)} test cases.")

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
