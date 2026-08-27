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

r"""Real-time agent thought streaming monitor (v2 — N-agent).

Streams the trajectory of N agents to stderr in real-time,
detects completion and failure conditions, saves final conversations,
and exits with a meaningful return code.

Usage:
    python3 monitor_session_test.py \
        --ports 15387,15388 \
        --csrf_token standalone-test-token \
        --agent_ids <id1>,<id2> \
        --run_dir /tmp/ztab_runs/...

Design doc: docs/ztab_harness_v2_design_2026_06_24.md
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request



# --- B2: Import shared helpers from harness_lib ---
from harness_lib import detect_active_timer
from harness_lib import download_conversation
from harness_lib import extract_step_text
from harness_lib import format_and_print_step
from harness_lib import get_agent_last_step
from harness_lib import get_agent_status
from harness_lib import ls_request
from harness_lib import STATUS_NAMES
from harness_lib import STATUS_STR_TO_INT
from harness_lib import stream_agent_thoughts
from harness_lib import extract_field_from_step
from harness_lib import extract_invitation_token

# Alias for backward compatibility within this file.
_detect_active_timer = detect_active_timer


# ---------------------------------------------------------------------------
# Final Conversation Save (via Connect HTTP RPC)
# ---------------------------------------------------------------------------


def save_final_conversation(
    port, csrf_token, cascade_id, label, run_dir
):
  """Download and save the full agent conversation via Connect HTTP RPC.

  Uses download_conversation helper to fetch steps and write them in
  compatible Go printStep format.
  """
  # Map label -> filename: Agent 1 -> agents/1/conversation.json
  agent_num = label.split()[-1]  # "Agent 1" -> "1"
  filepath = os.path.join(run_dir, "agents", agent_num, "conversation.json")
  try:
    download_conversation(port, csrf_token, cascade_id, filepath)
    print(f"  [{label}] Conversation saved to {filepath}", file=sys.stderr)
  except Exception as e:
    print(f"  [{label}] ERROR saving conversation: {e}", file=sys.stderr)



def _scan_agent_for_session_metadata(agent, label, old_last_step):
  """Scan newly streamed steps for invitation_token and participant_token.

  Returns a tuple of (invitation_token, creator_token) if found, else (None, None).
  """
  invitation_token = None
  creator_token = None
  try:
    if os.path.exists(agent["log_file"]):
      with open(agent["log_file"], "r") as f:
        lines = f.readlines()
        new_lines = lines[old_last_step:]
        for line in new_lines:
          step = json.loads(line.strip())
          sid = extract_invitation_token(
              step,
              tool_names="ztab_create_session",
              search_args=False,
              search_response=True,
          )
          if sid:
            invitation_token = sid
          tok = extract_field_from_step(
              step,
              "participant_token",
              tool_names="ztab_create_session",
              search_args=False,
              search_response=True,
          )
          if tok:
            creator_token = tok
  except Exception as e:
    print(
        f"  [{label}] [Monitor] WARNING failed to parse new steps: {e}",
        file=sys.stderr,
    )
  return invitation_token, creator_token


def _connect_to_tee(
    tee_host, tee_port, verifier,
    expected_digest=None,
    expected_project_id=None,
    expected_service_account=None,
    min_cs_version=None,
):
  """Establish a gRPC channel to the TEE server.

  Returns (channel, stub) or raises on error.
  """
  if verifier == "noop":
    policy = NoopPolicy()
  else:
    digests = frozenset([expected_digest]) if expected_digest else frozenset()
    kwargs = {
        "expected_image_digests": digests,
        "allow_debug": True,
    }
    if expected_project_id:
        kwargs["expected_project_id"] = expected_project_id
    if expected_service_account:
        kwargs["expected_service_account"] = expected_service_account
    if min_cs_version is not None:
        kwargs["min_cs_version"] = min_cs_version
    policy = ItaPolicy(**kwargs)
  verifier_obj = get_verifier(policy)
  channel = ZtabChannel(host=tee_host, port=tee_port, verifier=verifier_obj)
  grpc_chan = channel.connect()
  stub = session_manager_pb2_grpc.AgentBrokerServiceStub(grpc_chan)
  print(
      f"  [Monitor] Connected to TEE server {tee_host}:{tee_port} for direct"
      f" verification.",
      file=sys.stderr,
  )
  return channel, stub


def _poll_tee_status(stub, creator_token):
  """Query TEE server for session state.

  Returns session_manager_pb2.SessionState enum, or raises on failure.
  """
  req = session_manager_pb2.GetSessionStatusRequest(
      participant_token=creator_token
  )
  resp = stub.GetSessionStatus(req)
  state_name = session_manager_pb2.SessionState.Name(resp.state)
  ts_str = time.strftime("%H:%M:%S")
  print(
      f"  [{ts_str}] [TEE Server] Live state: {state_name} "
      f"(joined={resp.participants_joined}, accepted={resp.participants_accepted}, "
      f"inputs={resp.inputs_received})",
      file=sys.stderr,
  )
  return resp.state


def _nudge_agent_if_stalled(agent, label, csrf_token, nudge_text):
  """Send a nudge message to the agent's Language Server port."""
  ts_str = time.strftime("%H:%M:%S")
  print(
      f"  [{label}] [{ts_str}] [Monitor] Stall detected. Sending nudge...",
      file=sys.stderr,
  )
  try:
    ls_request(
        agent["port"],
        csrf_token,
        "SendUserCascadeMessage",
        {
            "cascade_id": agent["id"],
            "items": [{"text": nudge_text}],
        },
    )
    return True
  except Exception as e:
    print(f"  [{label}] [Monitor] ERROR sending nudge: {e}", file=sys.stderr)
    return False


# ---------------------------------------------------------------------------
# Failure Detection (Python port of bash check_agent_done)
# ---------------------------------------------------------------------------


def _check_closed_in_tool_responses(run_dir, agent_num):
  """F8/B5: Structurally check if a CLOSED/ABORTED state appears in tool responses.

  Parses thoughts.jsonl and looks for CLOSED/ABORTED in the `state` field
  of ztab_get_result and ztab_get_session_status tool responses. Falls back
  to substring matching only if JSON parsing fails.

  Args:
    run_dir: Path to the test run directory.
    agent_num: Agent number (string or int).

  Returns:
    Tuple of (found_closed: bool, found_aborted: bool).
  """
  thoughts_path = os.path.join(
      run_dir, "agents", str(agent_num), "thoughts.jsonl"
  )
  found_closed = False
  found_aborted = False
  if not os.path.exists(thoughts_path):
    return False, False
  try:
    with open(thoughts_path) as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        try:
          step = json.loads(line)
        except json.JSONDecodeError:
          continue
        step_type = step.get("type", "")
        if step_type != "CORTEX_STEP_TYPE_MCP_TOOL":
          continue
        mt = step.get("mcpTool", {})
        tc = mt.get("toolCall", {})
        name = tc.get("name", "")
        if name not in ("ztab_get_result", "ztab_get_session_status"):
          continue

        # B5: Try to parse the response as JSON and check the state field.
        result_parsed = False
        for field in ("resultString", "response", "content"):
          raw = mt.get(field, step.get(field, ""))
          if not raw:
            continue
          text = raw if isinstance(raw, str) else json.dumps(raw)
          # Try to parse as JSON to extract the state field.
          try:
            parsed = json.loads(text) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
              state = parsed.get("state", "")
              if state == "CLOSED":
                found_closed = True
                result_parsed = True
              elif state == "ABORTED":
                found_aborted = True
                result_parsed = True
          except (json.JSONDecodeError, TypeError):
            pass

        # Fallback: if JSON parsing didn't yield a state field, use substring
        # matching on the raw text (but only for ztab_get_result, not
        # ztab_get_session_status, to minimize false positives).
        if not result_parsed and name == "ztab_get_result":
          result_text = ""
          for field in ("resultString", "response", "content"):
            raw = mt.get(field, step.get(field, ""))
            if isinstance(raw, dict):
              result_text += json.dumps(raw)
            elif isinstance(raw, str):
              result_text += raw
          if "CLOSED" in result_text:
            found_closed = True
          if "ABORTED" in result_text:
            found_aborted = True
  except Exception:
    pass
  return found_closed, found_aborted


def check_agent_result(
    label, run_dir, checked_tee=False, tee_session_closed=False
):
  """Check if an agent's conversation indicates pass or fail.

  F8: Uses structural tool-response parsing for CLOSED/ABORTED detection
  instead of loose regex matching on raw conversation text (which caused
  false positives from prompt instructions containing 'state.*CLOSED').

  Returns a dict with:
    - passed: bool
    - failure_reason: str or None
  """
  agent_num = label.split()[-1]  # "Agent 1" -> "1"
  filepath = os.path.join(run_dir, "agents", agent_num, "conversation.json")

  try:
    with open(filepath) as f:
      content = f.read()
  except FileNotFoundError:
    return {"passed": False, "failure_reason": "Conversation not saved"}

  if not content.strip():
    return {"passed": False, "failure_reason": "Conversation file is empty"}

  # P1-2: CLI fallback & Process killing (scoped to run_command blocks)
  # Parse conversation into step blocks and scan only command execution steps
  # to avoid false-positives matching prompt instructions.
  has_cli_fallback = False
  has_pkill_fallback = False
  blocks = content.split("=" * 60)
  for block in blocks:
    lines = [l.strip() for l in block.splitlines() if l.strip()]
    if not lines:
      continue
    header = lines[0]
    if "run_command" in header:
      body = "\n".join(lines[1:])
      if "cli.py" in body:
        has_cli_fallback = True
      if re.search(r"pkill|kill.*mcp_server", body):
        has_pkill_fallback = True

  if has_cli_fallback:
    return {
        "passed": False,
        "failure_reason": "CLI fallback — agent executed cli.py",
    }
  if has_pkill_fallback:
    return {
        "passed": False,
        "failure_reason": "Agent killed system processes (pkill)",
    }

  # Direct TEE server verification (Point 1 & 2)
  if checked_tee and not tee_session_closed:
    return {
        "passed": False,
        "failure_reason": (
            "TEE session did not reach CLOSED state on server"
        ),
    }

  # Success patterns — multi-agent (calendar overlap results)
  if re.search(r'"2026-07-1[56]', content):
    return {"passed": True, "failure_reason": None}
  if re.search(
      r"overlap|common.*slot|meeting.*scheduled|intersection",
      content,
      re.IGNORECASE,
  ):
    return {"passed": True, "failure_reason": None}

  # Success patterns — 1-agent bootstrap (test_connection, echo)
  if re.search(r"test_connection.*success|Echo:|echo.*response", content, re.IGNORECASE):
    return {"passed": True, "failure_reason": None}

  # F8: Check for CLOSED/ABORTED structurally via tool responses.
  # This avoids matching prompt text like "target is a CLOSED state".
  tool_closed, tool_aborted = _check_closed_in_tool_responses(
      run_dir, agent_num
  )
  if tool_closed:
    return {"passed": True, "failure_reason": None}
  if tool_aborted:
    return {"passed": False, "failure_reason": "Session ABORTED in tool response"}

  # Failure patterns
  if re.search(r"error_code|processing failed", content, re.IGNORECASE):
    return {"passed": False, "failure_reason": "Agent reported error"}

  if "Status: idle" in content:
    return {
        "passed": False,
        "failure_reason": "Agent went idle without valid result",
    }

  return {
      "passed": False,
      "failure_reason": "No recognizable result pattern found",
  }


# ---------------------------------------------------------------------------
# Main Monitoring Loop
# ---------------------------------------------------------------------------


def monitor_agents(
    ports,
    csrf_token,
    agent_ids,
    run_dir,
    timeout_secs=900,
    poll_interval=10,
    tee_host="",
    tee_port=0,
    verifier="noop",
    expected_digest=None,
    expected_project_id=None,
    expected_service_account=None,
    min_cs_version=None,
):
  if tee_host and tee_port:
    # Set up paths and import gRPC/ZTAB packages conditionally
    import sys

    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    AGENT_DIR = os.path.normpath(os.path.join(SCRIPT_DIR, "../../agent"))
    if AGENT_DIR not in sys.path:
      sys.path.insert(0, AGENT_DIR)
    pb2_path = os.path.join(AGENT_DIR, "pb2")
    if pb2_path not in sys.path:
      sys.path.insert(0, pb2_path)

    global grpc, ZtabChannel, get_verifier, ItaPolicy, NoopPolicy, session_manager_pb2, session_manager_pb2_grpc
    import grpc
    from client import ZtabChannel
    from verifier_factory import get_verifier
    from verifier_policy import ItaPolicy, NoopPolicy
    import session_manager_pb2
    import session_manager_pb2_grpc
  """Stream thoughts for all agents until all finish.

  Args:
    ports: list of int (per-agent LS ports)
    agent_ids: list of str (per-agent cascade IDs)

  Returns a dict mapping label -> result dict with:
    - "passed": bool
    - "failure_reason": str or None
  """
  agents = {}
  for i, (port, cascade_id) in enumerate(zip(ports, agent_ids), 1):
    label = f"Agent {i}"
    agents[label] = {
        "id": cascade_id,
        "port": port,
        "last_step": 0,
        "done": False,
        "idle_since": None,
        "timer_scanned_step": 0,  # B1: Track incremental timer scan position
        "log_file": os.path.join(run_dir, "agents", str(i), "thoughts.jsonl"),
    }
  IDLE_GRACE_SECS = 30  # Base grace; extended dynamically by F7 timer detection
  invitation_token = None
  creator_token = None
  creator_label = None
  tee_stub = None
  tee_channel = None
  tee_state = None
  nudged_creator = False
  start_time = time.time()

  ts = time.strftime("%H:%M:%S")
  print(
      f"  [{ts}] Starting real-time monitoring of {len(agents)} agent(s) "
      f"(timeout={timeout_secs}s, poll={poll_interval}s)",
      file=sys.stderr,
  )

  while time.time() - start_time < timeout_secs:
    for label, agent in agents.items():
      if agent["done"]:
        continue

      old_last_step = agent["last_step"]
      try:
        new_step, info = stream_agent_thoughts(
            agent["port"],
            csrf_token,
            agent["id"],
            label,
            agent["last_step"],
            agent["log_file"],
        )
      except Exception as e:
        ts = time.strftime("%H:%M:%S")
        print(
            f"  [{label}] [{ts}] WARNING: LS request failed: {e} — will retry",
            file=sys.stderr,
        )
        continue
      steps_changed = (new_step > agent["last_step"])
      agent["last_step"] = new_step

      if steps_changed:
        agent["idle_since"] = None
        agent["timer_grace"] = IDLE_GRACE_SECS
        agent["done"] = False

        sid, tok = _scan_agent_for_session_metadata(
            agent, label, old_last_step
        )
        if sid:
          invitation_token = sid
          print(
              f"  [Monitor] Discovered Invitation Token: {invitation_token}",
              file=sys.stderr,
          )
        if tok:
          creator_token = tok
          creator_label = label  # Dynamically set who the creator is!
          print(
              f"  [Monitor] Discovered Creator {label} Participant Token:"
              f" {creator_token}",
              file=sys.stderr,
          )

      if info["status"] in (1, 3) and info["num_steps"] > 0:
        if agent["idle_since"] is None:
          agent["idle_since"] = time.time()
          # F7: Check if agent has active timer and extend grace period.
          timer_secs = _detect_active_timer(
              agent["port"], csrf_token, agent["id"],
              agent["timer_scanned_step"], agent["last_step"]
          )
          if timer_secs > 0:
            agent["timer_grace"] = timer_secs + 30  # timer + 30s buffer
            agent["timer_scanned_step"] = agent["last_step"]  # B1: advance
            print(
                f"  [{label}] [{time.strftime('%H:%M:%S')}] Active timer "
                f"detected ({timer_secs}s) — extending grace to "
                f"{agent['timer_grace']}s",
                file=sys.stderr,
            )
          else:
            agent["timer_grace"] = IDLE_GRACE_SECS
        else:
          effective_grace = agent.get("timer_grace", IDLE_GRACE_SECS)
          if time.time() - agent["idle_since"] > effective_grace:
            agent["done"] = True
            print(
                f"  [{label}] DONE ({info['status_name']} after"
                f" {info['num_steps']} steps, grace={effective_grace}s)",
                file=sys.stderr,
            )
      else:
        agent["idle_since"] = None
        agent["timer_grace"] = IDLE_GRACE_SECS
        agent["done"] = False

    # Direct TEE server verification (Point 1 & 2)
    if tee_host and tee_port and invitation_token and creator_token:
      if tee_stub is None:
        tee_channel, tee_stub = _connect_to_tee(
            tee_host,
            tee_port,
            verifier,
            expected_digest=expected_digest,
            expected_project_id=expected_project_id,
            expected_service_account=expected_service_account,
            min_cs_version=min_cs_version,
        )

      if tee_stub is not None:
        try:
          tee_state = _poll_tee_status(tee_stub, creator_token)
        except grpc.RpcError as e:
          if e.code() == grpc.StatusCode.UNAVAILABLE:
            print(
                "  [Monitor] TEE cert rotated, reconnecting...",
                file=sys.stderr,
            )
            if tee_channel:
              tee_channel.close()
            tee_channel, tee_stub = _connect_to_tee(
                tee_host, tee_port, verifier, expected_digest=expected_digest
            )
            tee_state = _poll_tee_status(
                tee_stub, creator_token)
          else:
            raise

    # Stall detection and creator nudging (Point 4)
    if creator_label and not nudged_creator and invitation_token is not None:
      creator_agent = agents.get(creator_label)
      if (
          creator_agent
          and not creator_agent["done"]
          and creator_agent["idle_since"] is not None
      ):
        idle_duration = time.time() - creator_agent["idle_since"]
        if idle_duration > 10:
          should_nudge = True
          if tee_host and tee_port:
            should_nudge = (tee_state is not None) and (tee_state in (
                session_manager_pb2.SessionState.OPEN,
                session_manager_pb2.SessionState.SEALED,
            ))

          if should_nudge:
            nudge_text = (
                "I have shared the session ID with the other participant."
                " Please proceed with polling the session status to check when"
                " they join."
            )
            success = _nudge_agent_if_stalled(
                creator_agent, creator_label, csrf_token, nudge_text
            )
            if success:
              nudged_creator = True
              creator_agent["idle_since"] = None
              creator_agent["done"] = False
              creator_agent["timer_grace"] = IDLE_GRACE_SECS

    if all(a["done"] for a in agents.values()):
      break

    time.sleep(poll_interval)

  elapsed = int(time.time() - start_time)
  if not all(a["done"] for a in agents.values()):
    ts = time.strftime("%H:%M:%S")
    not_done = [l for l, a in agents.items() if not a["done"]]
    print(
        f"  [{ts}] TIMEOUT after {elapsed}s — still running: "
        f"{', '.join(not_done)}",
        file=sys.stderr,
    )

  # Save final conversations
  for label, agent in agents.items():
    save_final_conversation(
        agent["port"], csrf_token, agent["id"], label, run_dir
    )

  # Run failure detection
  results = {}
  for label, agent in agents.items():
    results[label] = check_agent_result(
        label,
        run_dir,
        checked_tee=bool(tee_host and tee_port and invitation_token is not None),
        tee_session_closed=(
            tee_stub is not None
            and tee_state == session_manager_pb2.SessionState.CLOSED
        ),
    )

  return results


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------


def main():
  parser = argparse.ArgumentParser(
      description="Real-time agent thought streaming monitor (v2 N-agent)"
  )
  parser.add_argument(
      "--ports",
      required=True,
      help="Comma-separated LS ports (one per agent)",
  )
  parser.add_argument("--csrf_token", required=True, help="LS CSRF token")
  parser.add_argument(
      "--agent_ids",
      required=True,
      help="Comma-separated agent cascade IDs",
  )
  parser.add_argument(
      "--run_dir", required=True, help="Directory for logs and output files"
  )
  parser.add_argument(
      "--tee_host",
      default="",
      help="TEE server host address for direct validation",
  )
  parser.add_argument(
      "--tee_port",
      type=int,
      default=0,
      help="TEE server port for direct validation",
  )
  parser.add_argument(
      "--verifier",
      default="noop",
      help="Expected verifier (noop/ita) for TEE connection",
  )
  parser.add_argument(
      "--expected_digest",
      default=None,
      help="Expected container image digest for ITA verifier",
  )
  parser.add_argument(
      "--expected_project_id",
      default=None,
      help="Expected GCP project ID",
  )
  parser.add_argument(
      "--expected_service_account",
      default=None,
      help="Expected GCP service account",
  )
  parser.add_argument(
      "--min_cs_version",
      type=int,
      default=None,
      help="Minimum CS version",
  )
  parser.add_argument(
      "--timeout",
      type=int,
      default=900,
      help="Timeout in seconds (default: 900)",
  )
  parser.add_argument(
      "--poll_interval",
      type=int,
      default=10,
      help="Poll interval in seconds (default: 10)",
  )
  args = parser.parse_args()

  ports = [int(p) for p in args.ports.split(",")]
  agent_ids = [a.strip() for a in args.agent_ids.split(",")]

  if len(ports) != len(agent_ids):
    print(
        f"ERROR: {len(ports)} ports but {len(agent_ids)} agent IDs provided.",
        file=sys.stderr,
    )
    sys.exit(1)

  os.makedirs(args.run_dir, exist_ok=True)

  results = monitor_agents(
      ports=ports,
      csrf_token=args.csrf_token,
      agent_ids=agent_ids,
      run_dir=args.run_dir,
      timeout_secs=args.timeout,
      poll_interval=args.poll_interval,
      tee_host=args.tee_host,
      tee_port=args.tee_port,
      verifier=args.verifier,
      expected_digest=args.expected_digest,
      expected_project_id=args.expected_project_id,
      expected_service_account=args.expected_service_account,
      min_cs_version=args.min_cs_version,
  )

  print(f"\n{'='*50}", file=sys.stderr)
  print("Monitor Summary:", file=sys.stderr)
  all_passed = True
  for label, result in results.items():
    if result["passed"]:
      print(f"  {label}: PASS", file=sys.stderr)
    else:
      print(f"  {label}: FAIL — {result['failure_reason']}", file=sys.stderr)
      all_passed = False
  print(f"{'='*50}", file=sys.stderr)

  sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
  main()
