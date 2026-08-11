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
import uuid

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
        invitation_token = create_resp.invitation_token
        creator_token = create_resp.participant_token
        logging.info(f"Session created, invitation_token: {invitation_token[:16]}...")

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
                pb2.JoinSessionRequest(invitation_token=invitation_token))
            participant_tokens.append(join_resp.participant_token)
            logging.info(f"Participant {i+1} joined.")

        try:
            # 3. Accept Policy — all participants.
            creator_stub.AcceptPolicy(pb2.AcceptPolicyRequest(
                participant_token=creator_token))
            logging.info("Creator accepted policy.")

            for i, (stub, token) in enumerate(
                    zip(participant_stubs, participant_tokens[1:])):
                stub.AcceptPolicy(pb2.AcceptPolicyRequest(
                    participant_token=token))
                logging.info(f"Participant {i+2} accepted policy.")

            # Verify state is SEALED.
            status_resp = creator_stub.GetSessionStatus(
                pb2.GetSessionStatusRequest(
                    participant_token=creator_token))
            state_name = pb2.SessionState.Name(status_resp.state)
            logging.info(f"Session state after acceptance: {state_name}")
            if status_resp.state != pb2.SEALED:
                return False, f"Expected SEALED state, got {state_name}"

            # 4. Submit Inputs.
            creator_stub.SubmitInput(pb2.SubmitInputRequest(
                participant_token=creator_token,
                input_json=json.dumps(inputs[0]),
            ))
            logging.info(f"Creator submitted input.")

            for i, (stub, token) in enumerate(
                    zip(participant_stubs, participant_tokens[1:])):
                stub.SubmitInput(pb2.SubmitInputRequest(
                    participant_token=token,
                    input_json=json.dumps(inputs[i + 1]),
                ))
                logging.info(f"Participant {i+2} submitted input.")

            # 5. Poll for Result.
            logging.info("Polling for result...")
            result_req = pb2.GetResultRequest(
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


def run_admission_control_tests(args):
    """Run wire-level admission control and idempotency tests against live TEE server."""
    logging.info("=== Running Admission Control Wire-Level Tests ===")
    verifier = get_verifier(args.verifier, args.expected_digest, args.allow_debug_tee)
    passed = 0
    failed = 0

    with ZtabChannel(args.host, args.port, verifier) as channel:
        stub = pb2_grpc.AgentBrokerServiceStub(channel.grpc_channel)

        # 1. Creator Token Gating Tests (if creator_token is specified on server).
        if args.creator_token:
            # Test 1A: Missing token header rejected with PERMISSION_DENIED.
            try:
                stub.CreateSession(
                    pb2.CreateSessionRequest(
                        policy=pb2.SessionPolicy(
                            policy_class="ScheduleOverlap",
                            expected_participants=2,
                        )
                    )
                )
                logging.error("FAIL: Missing creator_token was accepted!")
                failed += 1
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.PERMISSION_DENIED:
                    logging.info("PASS: Missing creator_token rejected with PERMISSION_DENIED.")
                    passed += 1
                else:
                    logging.error(f"FAIL: Expected PERMISSION_DENIED, got {e.code()}")
                    failed += 1

            # Test 1B: Wrong token header rejected with PERMISSION_DENIED.
            try:
                stub.CreateSession(
                    pb2.CreateSessionRequest(
                        policy=pb2.SessionPolicy(
                            policy_class="ScheduleOverlap",
                            expected_participants=2,
                        )
                    ),
                    metadata=[("x-ztab-creator-token", "wrong-secret-token")],
                )
                logging.error("FAIL: Wrong creator_token was accepted!")
                failed += 1
            except grpc.RpcError as e:
                if e.code() == grpc.StatusCode.PERMISSION_DENIED:
                    logging.info("PASS: Wrong creator_token rejected with PERMISSION_DENIED.")
                    passed += 1
                else:
                    logging.error(f"FAIL: Expected PERMISSION_DENIED, got {e.code()}")
                    failed += 1

        # 2. Authorized CreateSession with Nonce & Replay.
        metadata = [("x-ztab-creator-token", args.creator_token)] if args.creator_token else None
        create_nonce = str(uuid.uuid4())
        policy = pb2.SessionPolicy(
            policy_class="ScheduleOverlap",
            expected_participants=2,
            timeout_seconds=300,
        )

        try:
            resp1 = stub.CreateSession(
                pb2.CreateSessionRequest(policy=policy, client_nonce=create_nonce),
                metadata=metadata,
            )
            logging.info("PASS: CreateSession with valid nonce succeeded.")
            passed += 1
        except Exception as e:
            logging.error(f"FAIL: CreateSession failed: {e}")
            return False, f"{passed} passed, {failed+1} failed"

        # Replay identical CreateSession with same nonce.
        try:
            resp2 = stub.CreateSession(
                pb2.CreateSessionRequest(policy=policy, client_nonce=create_nonce),
                metadata=metadata,
            )
            if (
                resp1.invitation_token == resp2.invitation_token
                and resp1.participant_token == resp2.participant_token
                and resp1.state == resp2.state
            ):
                logging.info("PASS: CreateSession idempotent replay returned identical tokens.")
                passed += 1
            else:
                logging.error("FAIL: CreateSession replay returned mismatched tokens.")
                failed += 1
        except Exception as e:
            logging.error(f"FAIL: CreateSession replay failed: {e}")
            failed += 1

        # Replay same nonce with altered policy -> must fail INVALID_ARGUMENT.
        try:
            altered_policy = pb2.SessionPolicy(
                policy_class="ScheduleOverlap",
                expected_participants=3,
                timeout_seconds=300,
            )
            stub.CreateSession(
                pb2.CreateSessionRequest(policy=altered_policy, client_nonce=create_nonce),
                metadata=metadata,
            )
            logging.error("FAIL: Replay with altered policy was accepted!")
            failed += 1
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.INVALID_ARGUMENT:
                logging.info("PASS: CreateSession replay with altered policy rejected with INVALID_ARGUMENT.")
                passed += 1
            else:
                logging.error(f"FAIL: Expected INVALID_ARGUMENT, got {e.code()}")
                failed += 1

        # 3. JoinSession Nonce & Replay.
        join_nonce = str(uuid.uuid4())
        invitation_token = resp1.invitation_token
        creator_token = resp1.participant_token

        # Invalid nonce format -> INVALID_ARGUMENT.
        try:
            stub.JoinSession(
                pb2.JoinSessionRequest(
                    invitation_token=invitation_token,
                    client_nonce="not-a-valid-uuid",
                )
            )
            logging.error("FAIL: Invalid UUID nonce accepted for JoinSession!")
            failed += 1
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.INVALID_ARGUMENT:
                logging.info("PASS: Invalid UUID nonce rejected with INVALID_ARGUMENT.")
                passed += 1
            else:
                logging.error(f"FAIL: Expected INVALID_ARGUMENT, got {e.code()}")
                failed += 1

        # Valid JoinSession with nonce.
        try:
            join1 = stub.JoinSession(
                pb2.JoinSessionRequest(
                    invitation_token=invitation_token,
                    client_nonce=join_nonce,
                )
            )
            joiner_token = join1.participant_token
            logging.info("PASS: JoinSession with valid nonce succeeded.")
            passed += 1
        except Exception as e:
            logging.error(f"FAIL: JoinSession failed: {e}")
            return False, f"{passed} passed, {failed+1} failed"

        # Replay JoinSession with same nonce -> returns same participant token.
        try:
            join2 = stub.JoinSession(
                pb2.JoinSessionRequest(
                    invitation_token=invitation_token,
                    client_nonce=join_nonce,
                )
            )
            if join1.participant_token == join2.participant_token:
                logging.info("PASS: JoinSession replay returned identical participant token.")
                passed += 1
            else:
                logging.error("FAIL: JoinSession replay returned different token.")
                failed += 1
        except Exception as e:
            logging.error(f"FAIL: JoinSession replay failed: {e}")
            failed += 1

        # 4. AcceptPolicy Idempotency.
        try:
            stub.AcceptPolicy(pb2.AcceptPolicyRequest(participant_token=creator_token))
            stub.AcceptPolicy(pb2.AcceptPolicyRequest(participant_token=joiner_token))
            # Duplicate call
            stub.AcceptPolicy(pb2.AcceptPolicyRequest(participant_token=joiner_token))
            logging.info("PASS: Duplicate AcceptPolicy succeeded idempotently.")
            passed += 1
        except Exception as e:
            logging.error(f"FAIL: AcceptPolicy idempotency failed: {e}")
            failed += 1

        # 5. SubmitInput Idempotency (Content Match vs Content Mismatch).
        sample_input = '{"available_slots": ["2026-07-15T10:00:00Z"]}'
        altered_input = '{"available_slots": ["2026-07-15T14:00:00Z"]}'

        try:
            stub.SubmitInput(
                pb2.SubmitInputRequest(
                    participant_token=creator_token,
                    input_json=sample_input,
                )
            )
            # Replay with identical input -> success
            stub.SubmitInput(
                pb2.SubmitInputRequest(
                    participant_token=creator_token,
                    input_json=sample_input,
                )
            )
            logging.info("PASS: Duplicate SubmitInput with identical content succeeded.")
            passed += 1
        except Exception as e:
            logging.error(f"FAIL: Duplicate SubmitInput with identical content failed: {e}")
            failed += 1

        # Replay with altered input -> FAILED_PRECONDITION
        try:
            stub.SubmitInput(
                pb2.SubmitInputRequest(
                    participant_token=creator_token,
                    input_json=altered_input,
                )
            )
            logging.error("FAIL: Duplicate SubmitInput with altered content was accepted!")
            failed += 1
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.FAILED_PRECONDITION:
                logging.info("PASS: Duplicate SubmitInput with altered content rejected with FAILED_PRECONDITION.")
                passed += 1
            else:
                logging.error(f"FAIL: Expected FAILED_PRECONDITION, got {e.code()}")
                failed += 1

    logging.info(f"Admission Control Summary: {passed} passed, {failed} failed.")
    return failed == 0, f"{passed} passed, {failed} failed"


def main():
    parser = argparse.ArgumentParser(
        description="ZTAB Component Test: Session Lifecycle and Admission Control")
    parser.add_argument(
        "--scenario", default=None,
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
    parser.add_argument(
        "--creator_token", default="",
        help="Creator token for admission control")
    parser.add_argument(
        "--test_admission", action="store_true", default=False,
        help="Run admission control wire-level test suite")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if args.test_admission:
        ok, detail = run_admission_control_tests(args)
        sys.exit(0 if ok else 1)

    if not args.scenario:
        logging.error("Either --scenario or --test_admission is required.")
        sys.exit(1)

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
