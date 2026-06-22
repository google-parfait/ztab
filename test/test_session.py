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

"""ZTAB Component Test: Session Lifecycle via gRPC.

Drives the TEE server through the full session lifecycle using direct gRPC
calls — no agents, no MCP. Accepts a Scenario object that provides
the policy class, inputs, and result validation.

This is a COMPONENT-LEVEL test. It tests the TEE backend in isolation.
For the real end-to-end agent test, use test_cold_start.sh.

Prerequisites:
  - Python dependencies installed (grpcio, cryptography).
  - Proto stubs compiled in agent/pb2/.
  - ZTAB TEE server running (default: localhost:8000).

Usage:
    python3 -m test.test_session \\
        --scenario examples.calendar.scenario:CalendarScenario \\
        --host localhost --port 8000 --verifier noop \\
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
import time

# Setup python path to import agent modules and pb2 stubs.
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
    """Load a scenario class from a module:ClassName spec string.

    Args:
        scenario_spec: String in format 'module.path:ClassName'.

    Returns:
        An instance of the scenario class.
    """
    module_path, class_name = scenario_spec.rsplit(":", 1)
    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)
    return cls()


def run_test_case(scenario, test_case, args):
    """Run a single test case through the full session lifecycle.

    Returns:
        Tuple of (passed: bool, detail: str).
    """
    logging.info(f"--- Test case: {test_case.name} ---")

    verifier = get_verifier(args.verifier, args.expected_digest,
                            args.allow_debug_tee)

    inputs = test_case.inputs
    if len(inputs) != scenario.num_participants:
        return False, (f"Test case '{test_case.name}' has {len(inputs)} inputs "
                       f"but scenario requires {scenario.num_participants}")

    # Establish connection for Creator (participant 0).
    with ZtabChannel(args.host, args.port, verifier) as creator_channel:
        creator_stub = pb2_grpc.AgentBrokerServiceStub(
            creator_channel.grpc_channel)
        logging.info("Creator channel established.")

        # 1. Create Session.
        policy = pb2.SessionPolicy(
            policy_class=scenario.policy_class,
            expected_participants=scenario.num_participants,
            timeout_seconds=300,
        )
        logging.info(f"Creating session with policy_class={scenario.policy_class}...")
        create_resp = creator_stub.CreateSession(
            pb2.CreateSessionRequest(policy=policy))
        session_id = create_resp.session_id
        creator_token = create_resp.participant_token
        logging.info(f"Session created: {session_id}")

        # 2. Join remaining participants.
        participant_channels = []
        participant_stubs = []
        participant_tokens = [creator_token]

        for i in range(1, scenario.num_participants):
            ch = ZtabChannel(args.host, args.port, verifier)
            ch.__enter__()
            participant_channels.append(ch)
            stub = pb2_grpc.AgentBrokerServiceStub(ch.grpc_channel)
            participant_stubs.append(stub)

            join_resp = stub.JoinSession(
                pb2.JoinSessionRequest(session_id=session_id))
            participant_tokens.append(join_resp.participant_token)
            logging.info(f"Participant {i+1} joined.")

        try:
            # 3. Accept Policy — all participants.
            creator_stub.AcceptPolicy(pb2.AcceptPolicyRequest(
                session_id=session_id, participant_token=creator_token))
            logging.info("Creator accepted policy.")

            for i, (stub, token) in enumerate(
                    zip(participant_stubs, participant_tokens[1:])):
                stub.AcceptPolicy(pb2.AcceptPolicyRequest(
                    session_id=session_id, participant_token=token))
                logging.info(f"Participant {i+2} accepted policy.")

            # Verify state is SEALED.
            status_resp = creator_stub.GetSessionStatus(
                pb2.GetSessionStatusRequest(
                    session_id=session_id,
                    participant_token=creator_token))
            state_name = pb2.SessionState.Name(status_resp.state)
            logging.info(f"Session state after acceptance: {state_name}")
            if status_resp.state != pb2.SEALED:
                return False, f"Expected SEALED state, got {state_name}"

            # 4. Submit Inputs.
            creator_stub.SubmitInput(pb2.SubmitInputRequest(
                session_id=session_id,
                participant_token=creator_token,
                input_json=json.dumps(inputs[0]),
            ))
            logging.info(f"Creator submitted input.")

            for i, (stub, token) in enumerate(
                    zip(participant_stubs, participant_tokens[1:])):
                stub.SubmitInput(pb2.SubmitInputRequest(
                    session_id=session_id,
                    participant_token=token,
                    input_json=json.dumps(inputs[i + 1]),
                ))
                logging.info(f"Participant {i+2} submitted input.")

            # 5. Poll for Result.
            logging.info("Polling for result...")
            result_req = pb2.GetResultRequest(
                session_id=session_id,
                participant_token=creator_token,
            )

            for attempt in range(180):
                result_resp = creator_stub.GetResult(result_req)
                state_name = pb2.SessionState.Name(result_resp.state)

                if result_resp.state == pb2.CLOSED:
                    logging.info(f"Result received after {attempt+1} polls.")
                    allow = test_case.allow_subset or args.allow_subset
                    return scenario.validate_result(
                        result_resp.result_json,
                        test_case.expected_result,
                        allow_subset=allow,
                    )
                elif result_resp.state == pb2.ABORTED:
                    return False, f"Session ABORTED: {result_resp.error_detail}"

                if attempt % 10 == 0:
                    logging.info(f"Poll {attempt+1}: state={state_name}")
                time.sleep(1)

            return False, "Timeout waiting for result (180s)."

        finally:
            # Clean up participant channels.
            for ch in participant_channels:
                try:
                    ch.__exit__(None, None, None)
                except Exception:
                    pass


def main():
    parser = argparse.ArgumentParser(
        description="ZTAB Component Test: Session Lifecycle")
    parser.add_argument(
        "--scenario", required=True,
        help="Scenario spec in format 'module.path:ClassName' "
             "(e.g., examples.calendar.scenario:CalendarScenario)")
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
        ok, detail = run_test_case(scenario, tc, args)
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
