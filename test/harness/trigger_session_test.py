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

r"""Trigger an N-agent session test via LS HTTP API (v3 phased).

Two-phase execution:
  Phase 1: Start all agents, send install-only prompt, validate on disk.
  Phase 2: Start NEW cascades for each agent (fresh executor = ZTAB tools
           visible), send scenario prompts with staggered creator/joiner.

Usage:
    python3 trigger_session_test.py \
        --ports 15387,15388 \
        --csrf_token standalone-test-token \
        --workspace /path/to/workspace \
        --host 127.0.0.1 --tee_port 9002 \
        --skill_path <ztab_dir>/agent/SKILL.md \
        --run_dir /tmp/ztab_runs/... \
        --num_agents 2
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

from harness_lib import detect_active_timer
from harness_lib import extract_step_text
from harness_lib import format_and_print_step
from harness_lib import get_agent_last_step
from harness_lib import get_agent_status
from harness_lib import ls_request
from harness_lib import STATUS_NAMES
from harness_lib import STATUS_STR_TO_INT
from harness_lib import extract_invitation_token

def wait_for_mcp_server(port, csrf_token, server_name, timeout_secs=90):
  """Triggers LS config reload via RefreshMcpServers, then polls until READY.

  The LS McpManager has no file watcher on mcp_config.json. It only loads
  configs at startup and on explicit RefreshMcpServers RPC. After Phase 1
  writes mcp_config.json, we must call RefreshMcpServers to trigger
  McpManager.Load(), then poll GetMcpServerStates to confirm the MCP server
  process is healthy before starting Phase 2 cascades.

  Uses a 10s per-call socket timeout so that a single slow RPC cannot
  exhaust the total retry budget.
  """
  PER_CALL_TIMEOUT = 10  # seconds per ls_request call
  start_time = time.time()
  print(f"Waiting for MCP server '{server_name}' to be READY on port {port} "
        f"(budget={timeout_secs}s, per_call={PER_CALL_TIMEOUT}s)...",
        flush=True)

  # Step 1: Trigger explicit reload of mcp_config.json
  try:
    ls_request(port, csrf_token, "RefreshMcpServers", {},
               timeout=PER_CALL_TIMEOUT)
    print(f"Triggered RefreshMcpServers on port {port}", flush=True)
  except Exception as e:
    print(f"WARNING: RefreshMcpServers failed: {e}. Will retry in poll loop.", flush=True)

  # Step 2: Poll GetMcpServerStates (now the intersection can succeed)
  last_debug = 0
  while time.time() - start_time < timeout_secs:
    try:
      # Re-trigger refresh every 5s in case the first was too early
      elapsed = int(time.time() - start_time)
      if elapsed > 0 and elapsed % 5 == 0:
        try:
          refresh_resp = ls_request(port, csrf_token, "RefreshMcpServers", {},
                                    timeout=PER_CALL_TIMEOUT)
          print(f"[{elapsed}s] Re-triggered RefreshMcpServers: {refresh_resp}", flush=True)
        except Exception as re:
          print(f"[{elapsed}s] RefreshMcpServers retry failed: {re}", flush=True)
      resp = ls_request(port, csrf_token, "GetMcpServerStates", {},
                        timeout=PER_CALL_TIMEOUT)
      states = resp.get("states", [])
      # Debug: log raw response every 3s
      if elapsed - last_debug >= 3:
        state_summary = []
        for s in states:
          sp = s.get("spec", {})
          sn = sp.get("server_name") or sp.get("name") or sp.get("serverName") or "?"
          st = s.get("status", "?")
          if sn == "?":
            state_summary.append(f"UNKNOWN(spec_keys={list(sp.keys())}, spec={json.dumps(sp)[:200]})={st}")
          else:
            state_summary.append(f"{sn}={st}")
        print(f"[{elapsed}s] GetMcpServerStates: {len(states)} servers: [{', '.join(state_summary)}]", flush=True)
        last_debug = elapsed
      for s in states:
        spec = s.get("spec", {})
        name = spec.get("server_name") or spec.get("serverName") or spec.get("name")
        if name == server_name:
          status = s.get("status")
          if status in ("READY", "MCP_SERVER_STATUS_READY", 2):
            print(f"✅ MCP server '{server_name}' is READY.", flush=True)
            return True
          elif status in ("ERROR", "MCP_SERVER_STATUS_ERROR", 3):
            err_msg = s.get("error", "Unknown error")
            raise RuntimeError(f"MCP server '{server_name}' failed to load: {err_msg}")
          else:
            print(f"MCP server '{server_name}' status: {status}. Waiting...", flush=True)
    except urllib.error.HTTPError as e:
      print(f"LS connection error: {e}. Retrying...", flush=True)
    except Exception as e:
      if "failed to load" in str(e):
        raise
      print(f"Error checking MCP status: {e}. Retrying...", flush=True)
    time.sleep(1)
  raise TimeoutError(f"Timed out waiting {timeout_secs}s for MCP server '{server_name}' to be READY")

# Import calendar scenario data from the OSS repo.
_HARNESS_DIR = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ZTAB_DIR = os.path.abspath(os.path.join(_HARNESS_DIR, "..", ".."))
_ZTAB_DIR = os.environ.get("ZTAB_DIR", _DEFAULT_ZTAB_DIR)
if _ZTAB_DIR not in sys.path:
  sys.path.insert(0, _ZTAB_DIR)
from examples.calendar import test_data as calendar_data

# Invitation token extraction: The harness discovers the
# invitation_token by parsing the agent's trajectory in real-time.
# When Agent 1 calls ztab_create_session, the response contains
# the invitation_token. The harness extracts it from the streamed
# trajectory steps (no file writes needed).


# Alias: callers inside this file use the underscore-prefixed name.
_detect_active_timer = detect_active_timer


def start_agent(port, csrf_token, workspace_uri, prompt, model, label,
                sandbox_dir=None, skill_path=None, allow_ztab=True):
  """Start a cascade and send a prompt with MCP permissions.

  F6: Permissions are scoped to the agent's sandbox directory and skill path
  instead of wildcard (*), preventing sandbox escape.
  """
  # F6: Build scoped permission grants instead of wildcard.
  # Allow read/write only within the agent's sandbox and the skill directory.
  allow_grants = ["command(*)"]
  if allow_ztab:
    allow_grants.append("mcp(ztab/*)")
  if sandbox_dir:
    allow_grants.append(f"read_file({sandbox_dir})")
    allow_grants.append(f"write_file({sandbox_dir})")
  else:
    # Fallback to wildcard if sandbox_dir not provided (legacy callers)
    allow_grants.append("read_file(*)")
    allow_grants.append("write_file(*)")
  if skill_path:
    # Allow reading the skill directory containing SKILL.md
    skill_dir = os.path.dirname(skill_path)
    allow_grants.append(f"read_file({skill_dir})")

  print(f"Starting {label}...")
  start_resp = ls_request(
      port,
      csrf_token,
      "StartCascade",
      {
          "source": "CORTEX_TRAJECTORY_SOURCE_AGENT_API",
          "workspace_uris": [workspace_uri],
          "custom_agent_spec": {
              "coding_agent": {"google_mode": True},
              "command_execution_policy": "eager",
              "cascade_config": {
                  "conversation_history_config": {"enabled": True},
                  "planner_config": {
                      "plan_model": model,
                      "requested_model": {"model": model},
                      "tool_config": {
                          "permission_config": {
                              "effective_grants": {
                                  "allow": allow_grants,
                              },
                          },
                      },
                  },
              },
          },
      },
  )
  cascade_id = start_resp.get("cascadeId") or start_resp.get("cascade_id")
  if not cascade_id:
    print(f"ERROR starting {label}: {start_resp}", file=sys.stderr)
    sys.exit(1)
  print(f"  {label} Cascade ID: {cascade_id}")

  ls_request(
      port,
      csrf_token,
      "SendUserCascadeMessage",
      {
          "cascade_id": cascade_id,
          "items": [{"text": prompt}],
      },
  )
  print(f"  {label} prompt sent OK")
  return cascade_id



def monitor_agent_and_find_session(
    port, csrf_token, cascade_id, run_dir, timeout_secs=600
):
  """Monitor Agent 1 in real-time while waiting for session creation.

  Streams Agent 1's trajectory steps to stderr in real-time while also:
  1. Polling the LS for agent status every 10 seconds
  2. Extracting invitation_token from ztab_create_session trajectory steps
  3. Logging all status changes to $RUN_DIR/agents/1/monitor.log
  4. Early-aborting if the agent goes idle without creating a session
  5. Early-aborting if the agent errors out

  Falls back to reading invitation_token from Agent 1's trajectory if the
  initial extraction misses it.

  Returns: invitation_token (str) or None
  """
  monitor_log_path = (
      os.path.join(run_dir, "agents", "1", "monitor.log") if run_dir else None
  )
  thoughts_log_path = (
      os.path.join(run_dir, "agents", "1", "thoughts.jsonl")
      if run_dir
      else None
  )
  monitor_lines = []

  def mlog(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"{ts} {msg}"
    print(f"  [monitor] {line}", file=sys.stderr)
    monitor_lines.append(line)

  mlog(f"Starting real-time monitoring of Agent 1 ({cascade_id})")

  last_status = None
  last_step_count = 0
  last_seen_step = 0  # For incremental thought streaming
  last_timer_scanned_step = 0  # B1: Track how far we've scanned for timers
  idle_since = None
  IDLE_GRACE_SECS = 30  # Base grace; extended dynamically by F7 timer detection
  effective_grace = IDLE_GRACE_SECS  # Initialize to prevent UnboundLocalError

  invitation_token = None
  start_time = time.time()

  for _ in range(timeout_secs // 10):
    time.sleep(10)
    elapsed = int(time.time() - start_time)

    # 1. Poll agent status
    info = get_agent_status(port, csrf_token, cascade_id)
    status_name = info["status_name"]
    num_steps = info["num_steps"]

    # Log status changes
    if status_name != last_status or num_steps != last_step_count:
      mlog(
          f"[{elapsed}s] status={status_name} steps={num_steps}"
          f" (was {last_status}/{last_step_count})"
      )
      last_status = status_name
      last_step_count = num_steps
      idle_since = None  # Reset idle timer on any change
    else:
      # No change — periodic heartbeat
      if elapsed % 60 == 0:  # Log every 60s even if unchanged
        mlog(f"[{elapsed}s] status={status_name} steps={num_steps} (unchanged)")

    # 2. Check streamed trajectory steps for ztab_create_session response
    # (Primary method: no file writes needed from the agent)
    if num_steps > last_seen_step:
      try:
        steps_resp = get_agent_last_step(
            port, csrf_token, cascade_id, offset=last_seen_step
        )
        if steps_resp:
          for step in steps_resp.get("steps", []):
            format_and_print_step(step, "Agent 1", last_seen_step)
            if thoughts_log_path:
              with open(thoughts_log_path, "a") as f:
                f.write(json.dumps(step) + "\n")
            # Extract invitation_token from trajectory if this is a create_session call
            if not invitation_token:
              sid = extract_invitation_token(
                  step,
                  tool_names="ztab_create_session",
                  search_args=False,
                  search_response=True,
              )
              if sid:
                invitation_token = sid
                mlog(f"SESSION FOUND IN TRAJECTORY: {invitation_token} (after {elapsed}s)")
            last_seen_step += 1
      except Exception as e:
        mlog(f"[{elapsed}s] WARNING: step fetch failed: {e}")

    if invitation_token:
      break



    # 3. Check for error conditions
    if info["error"]:
      mlog(f"[{elapsed}s] LS API error: {info['error']}")

    # 4. Early abort: agent is IDLE with steps > 0 (it ran and stopped)
    if info["status"] == 1 and num_steps > 0:
      if idle_since is None:
        idle_since = time.time()
        # F7: Check for active timer and extend grace dynamically.
        timer_secs = _detect_active_timer(
            port, csrf_token, cascade_id, last_timer_scanned_step,
            last_seen_step
        )
        if timer_secs > 0:
          effective_grace = timer_secs + 30  # timer + 30s buffer
          last_timer_scanned_step = last_seen_step  # B1: advance scan cursor
          mlog(
              f"[{elapsed}s] Agent went IDLE with {num_steps} steps — "
              f"active timer ({timer_secs}s), grace period {effective_grace}s"
          )
        else:
          effective_grace = IDLE_GRACE_SECS
          mlog(
              f"[{elapsed}s] Agent went IDLE with {num_steps} steps — "
              f"grace period {IDLE_GRACE_SECS}s"
          )
      elif time.time() - idle_since > effective_grace:
        mlog(
            f"[{elapsed}s] EARLY ABORT: Agent IDLE for "
            f"{int(time.time() - idle_since)}s after {num_steps} steps "
            f"(grace={effective_grace}s) "
            "— it finished without creating a session"
        )
        # Dump any remaining un-streamed steps for diagnosis
        if num_steps > last_seen_step:
          steps_resp = get_agent_last_step(
              port, csrf_token, cascade_id, offset=last_seen_step
          )
          if steps_resp:
            for step in steps_resp.get("steps", []):
              format_and_print_step(step, "Agent 1", last_seen_step)
              last_seen_step += 1
        break
    else:
      idle_since = None
      effective_grace = IDLE_GRACE_SECS

  # Write monitor log
  if monitor_log_path:
    try:
      with open(monitor_log_path, "w") as f:
        f.write("\n".join(monitor_lines) + "\n")
    except Exception as e:
      print(f"WARNING: Could not write monitor log: {e}", file=sys.stderr)

  return invitation_token


def main():
  parser = argparse.ArgumentParser(
      description="Trigger ZTAB N-agent session test (v3 phased harness)"
  )
  parser.add_argument(
      "--ports",
      required=True,
      help="Comma-separated LS HTTP ports (one per agent)",
  )
  parser.add_argument("--csrf_token", default="standalone-test-token")
  parser.add_argument(
      "--workspace", required=True, help="ZTAB workspace path"
  )
  parser.add_argument("--host", default="127.0.0.1", help="TEE host")
  parser.add_argument("--tee_port", type=int, default=9002, help="TEE port")
  parser.add_argument("--skill_path", required=True, help="Path to SKILL.md")
  parser.add_argument(
      "--model", default="MODEL_PLACEHOLDER_M5", help="Model enum"
  )
  parser.add_argument(
      "--ztab_dir", default="", help="Path to ztab repo (for CLI)"
  )
  parser.add_argument(
      "--verifier",
      default="noop",
      choices=["noop", "ita"],
      help="Attestation verifier: 'noop' (default, no attestation) or 'ita' (production)",
  )
  parser.add_argument(
      "--expected_digest",
      default="",
      help="Expected container digest for TEE attestation",
  )
  parser.add_argument(
      "--tee_log",
      default="/tmp/ztab_tee_session_server.log",
      help="Path to the TEE server log file",
  )
  parser.add_argument(
      "--run_dir", required=True, help="Run directory for agent sandboxes"
  )
  parser.add_argument(
      "--num_agents", type=int, default=2, help="Number of agents"
  )
  parser.add_argument(
      "--phase1_only",
      action="store_true",
      help="Only run Phase 1 (install), skip session lifecycle",
  )
  parser.add_argument(
      "--creator_token",
      default="",
      help="Creator token for admission control (propagated to backends.json)",
  )
  args = parser.parse_args()

  ports = [int(p) for p in args.ports.split(",")]
  if len(ports) < args.num_agents:
    print(
        f"ERROR: {args.num_agents} agents requested but only {len(ports)} "
        "ports provided.",
        file=sys.stderr,
    )
    sys.exit(1)

  workspace_uri = f"file://{args.workspace}"
  run_dir = args.run_dir

  # Build thin TEE backend parameters
  digest_line = (
      f"    expected_digest: {args.expected_digest}\n"
      if args.expected_digest
      else ""
  )
  creator_line = (
      f"    creator_token: {args.creator_token}\n"
      if args.creator_token
      else ""
  )
  tee_params = (
      f"    host: {args.host}\n"
      f"    port: {args.tee_port}\n"
      f"    verifier: {args.verifier}\n"
      f"{digest_line}"
      f"{creator_line}"
      "    allow_debug_tee: true"
  )

  # ========================================
  # PHASE 1: Install ZTAB MCP server
  # ========================================
  print("=== PHASE 1: Install ZTAB on all agents ===")

  phase1_prompt = f"""Read the ZTAB skill at: {args.skill_path}
Follow Part 1 (Installation & Configuration) ONLY.

Your assignment:
  TEE backend:
    name: meeting-tee
{tee_params}

Install the ZTAB MCP server and configure the backend above.
Do NOT create or join any sessions.
When installation is complete, confirm with a brief summary of what you did.
"""

  # Start all agents and send Phase 1 prompt
  agent_ids = []
  for i in range(1, args.num_agents + 1):
    agent_sandbox = os.path.join(run_dir, "agents", str(i), "home")
    agent_id = start_agent(
        ports[i - 1],
        args.csrf_token,
        workspace_uri,
        phase1_prompt,
        args.model,
        f"Agent {i}",
        sandbox_dir=agent_sandbox,
        skill_path=args.skill_path,
        allow_ztab=False,
    )
    agent_ids.append(agent_id)

  # Wait for all agents to complete Phase 1 and validate installation
  print("\n=== Waiting for Phase 1 completion ===")
  expected_verifier = args.verifier
  phase1_ok = _wait_for_phase1_completion(
      ports[:args.num_agents],
      args.csrf_token,
      agent_ids,
      run_dir,
      args.host,
      args.tee_port,
      expected_verifier=expected_verifier,
      timeout_secs=300,
  )

  if not phase1_ok:
    # Dump trajectories even on failure for post-mortem analysis
    _dump_trajectories(ports[:args.num_agents], args.csrf_token,
                       agent_ids, run_dir)
    print("PHASE 1 FAILED: Installation validation did not pass.",
          file=sys.stderr)
    sys.exit(1)

  print("\n✅ Phase 1 complete — all agents installed ZTAB successfully.")

  # Dump Phase 1 trajectories so audit can analyze them
  _dump_trajectories(ports[:args.num_agents], args.csrf_token,
                     agent_ids, run_dir)

  if args.phase1_only:
    print(
        json.dumps({
            "phase": "phase1_only",
            "agent_conversation_ids": agent_ids,
            "result": "PASS",
        })
    )
    return

  # ========================================
  # PHASE 2: Session lifecycle (new cascades for fresh tool registry)
  # ========================================
  print("\n=== PHASE 2: Session lifecycle (new cascades) ===")

  # We start NEW cascades (StartCascade) for Phase 2 instead of sending
  # follow-up messages to the Phase 1 cascades. This is critical because:
  #   - SendUserCascadeMessage reuses the same CascadeExecutor
  #   - The executor keeps a static tool list from Phase 1 startup
  #   - In Phase 1, ZTAB wasn't installed, so call_mcp_tool and ztab_*
  #     tools were never registered
  #   - StartCascade creates a fresh executor -> PopulateEnv reads
  #     mcp_config.json -> ZTAB tools are now visible

  phase2_ids = []

  # Ensure ZTAB MCP server is fully loaded by Agent 1's LS before starting Creator cascade
  try:
    wait_for_mcp_server(ports[0], args.csrf_token, "ztab")
  except Exception as e:
    print(f"FAIL: Creator agent LS failed to load ZTAB MCP server: {e}", file=sys.stderr)
    _dump_trajectories(ports[:args.num_agents], args.csrf_token, agent_ids, run_dir)
    sys.exit(1)

  # Step 1: Start Phase 2 Creator cascade for Agent 1
  # F4: When num_agents==1, use expected_participants=2 and instruct
  # the agent to join its own session as the second participant.
  expected_p = max(2, args.num_agents)
  self_join_note = ""
  if args.num_agents == 1:
    self_join_note = ("\nYou are the only agent. After creating the session, "
                      "you must also join it yourself as the second "
                      "participant using the native MCP tool `ztab_join_session` "
                      "with the same invitation_token. Then accept the policy using the "
                      "native MCP tool `ztab_accept_policy`. "
                      "CRITICAL: You MUST use native ZTAB MCP tools (via call_mcp_tool) "
                      "for all session operations. Do NOT attempt to run shell commands "
                      "or use cli.py. "
                      "Since you are playing both roles, you must submit "
                      "input twice: once using the Creator's participant token "
                      "(which you got when creating the session), and once "
                      "using the Joiner's participant token (which you got "
                      "when joining the session). Both inputs must be submitted "
                      "to close the session.\n")

  phase2_creator = f"""The ZTAB MCP tools are now available.
Read the ZTAB skill at: {args.skill_path}
Follow Part 2 (Session Lifecycle) for the CREATOR role.
{self_join_note}
Your assignment:
  Role: CREATOR
  Session:
    policy_class: ScheduleOverlap
    expected_participants: {expected_p}
  Your data:
    {json.dumps({"available_slots": calendar_data.PARTICIPANT_A_SLOTS})}
"""
  print("  Starting Phase 2 Creator cascade for Agent 1...")
  agent1_sandbox = os.path.join(run_dir, "agents", "1", "home")
  phase2_creator_id = start_agent(
      ports[0], args.csrf_token, workspace_uri,
      phase2_creator, args.model, "Agent 1 (Phase 2 Creator)",
      sandbox_dir=agent1_sandbox,
      skill_path=args.skill_path,
  )
  phase2_ids.append(phase2_creator_id)

  # Step 2: Monitor Agent 1 (Phase 2 cascade) for invitation_token
  print("\n=== Monitoring Agent 1 for session creation ===")
  invitation_token = monitor_agent_and_find_session(
      port=ports[0],
      csrf_token=args.csrf_token,
      cascade_id=phase2_creator_id,
      run_dir=run_dir,
      timeout_secs=600,
  )

  if not invitation_token:
    final_info = get_agent_status(ports[0], args.csrf_token, phase2_creator_id)
    print(
        f"ERROR: Agent 1 did not create a session within 600s.",
        file=sys.stderr,
    )
    print(f"  Final status: {final_info['status_name']}", file=sys.stderr)
    print(f"  Total steps: {final_info['num_steps']}", file=sys.stderr)
    if final_info["error"]:
      print(f"  LS error: {final_info['error']}", file=sys.stderr)
    if final_info["num_steps"] > 0:
      last = get_agent_last_step(
          ports[0],
          args.csrf_token,
          phase2_creator_id,
          max(0, final_info["num_steps"] - 2),
      )
      if last:
        last_json = json.dumps(last, indent=2)[:3000]
        print(f"  Last steps:\n{last_json}", file=sys.stderr)
        try:
          with open(
              os.path.join(run_dir, "agents", "1", "last_steps.json"), "w"
          ) as f:
            json.dump(last, f, indent=2)
        except Exception:
          pass
    sys.exit(1)

  time.sleep(5)  # Brief stabilization pause

  # Step 3: Start Phase 2 Joiner cascades for agents 2..N
  if args.num_agents > 1:
    print(f"\n=== Starting Phase 2 Joiner cascades for {args.num_agents - 1}"
          " agent(s) ===")

  for i in range(2, args.num_agents + 1):
    # Ensure ZTAB MCP server is fully loaded by Joiner agent's LS before starting Joiner cascade
    try:
      wait_for_mcp_server(ports[i-1], args.csrf_token, "ztab")
    except Exception as e:
      print(f"FAIL: Joiner agent {i} LS failed to load ZTAB MCP server: {e}", file=sys.stderr)
      _dump_trajectories(ports[:args.num_agents], args.csrf_token, agent_ids, run_dir)
      sys.exit(1)

    phase2_joiner = f"""The ZTAB MCP tools are now available.
Read the ZTAB skill at: {args.skill_path}
Follow Part 2 (Session Lifecycle) for the JOINER role.

Your assignment:
  Role: JOINER
  TEE backend:
    name: meeting-tee
{tee_params}
  Session:
    invitation_token: {invitation_token}
  Your data:
    {json.dumps({"available_slots": calendar_data.PARTICIPANT_B_SLOTS})}
"""

    print(f"  Starting Phase 2 Joiner cascade for Agent {i}...")
    joiner_sandbox = os.path.join(run_dir, "agents", str(i), "home")
    phase2_joiner_id = start_agent(
        ports[i - 1], args.csrf_token, workspace_uri,
        phase2_joiner, args.model, f"Agent {i} (Phase 2 Joiner)",
        sandbox_dir=joiner_sandbox,
        skill_path=args.skill_path,
    )
    phase2_ids.append(phase2_joiner_id)

  # Dump Phase 2 trajectories will happen via monitor in test_cold_start.sh
  # But we also append Phase 2 cascade IDs to the output so the harness
  # can monitor them.

  print(f"\n{'='*50}")
  print(f"All {args.num_agents} agents started Phase 2 (new cascades).")
  for i, (p1, p2) in enumerate(zip(agent_ids, phase2_ids), 1):
    print(f"  Agent {i}: Phase1={p1}, Phase2={p2}")
  print(f"  Invitation token: {invitation_token}")
  print(f"{'='*50}")
  print(
      json.dumps({
          "agent_conversation_ids": agent_ids,
          "phase2_conversation_ids": phase2_ids,
          "invitation_token": invitation_token,
      })
  )

def _dump_trajectories(ports, csrf_token, cascade_ids, run_dir, mode="w"):
  """Dump all trajectory steps to thoughts.jsonl for each agent.

  Called after Phase 1 completes (pass or fail) so the audit script
  can analyze what the agent actually did.

  Args:
    mode: "w" for write (Phase 1, overwrites), "a" for append (Phase 2,
          appends to existing Phase 1 trajectory).
  """
  for i, (port, cid) in enumerate(zip(ports, cascade_ids), 1):
    agent_dir = os.path.join(run_dir, "agents", str(i))
    thoughts_path = os.path.join(agent_dir, "thoughts.jsonl")
    try:
      # Get total steps first
      info = get_agent_status(port, csrf_token, cid)
      total_steps = info["num_steps"]
      if total_steps == 0:
        print(f"  Agent {i}: no trajectory steps to dump", file=sys.stderr)
        continue

      # Fetch all steps
      steps_resp = get_agent_last_step(port, csrf_token, cid, offset=0)
      if steps_resp:
        steps = steps_resp.get("steps", [])
        with open(thoughts_path, mode) as f:
          for step in steps:
            f.write(json.dumps(step) + "\n")
        verb = "appended" if mode == "a" else "dumped"
        print(f"  Agent {i}: {verb} {len(steps)} trajectory steps "
              f"to thoughts.jsonl", file=sys.stderr)
      else:
        print(f"  Agent {i}: failed to fetch trajectory steps",
              file=sys.stderr)
    except Exception as e:
      print(f"  Agent {i}: trajectory dump error: {e}", file=sys.stderr)




def _verify_installation(run_dir, agent_num, expected_host, expected_port,
                         expected_verifier=None):
  """Validate Phase 1 completed correctly for one agent.

  Reads mcp_config.json to derive the venv path (not hardcoded).
  Optionally checks that backends.json verifier matches expected_verifier.
  Returns (success: bool, errors: list[str])
  """
  agent_home = os.path.join(run_dir, "agents", str(agent_num), "home")
  errors = []

  # Check 1: mcp_config.json contains "ztab" with valid command
  mcp_path = os.path.join(agent_home, ".gemini", "config", "mcp_config.json")
  ztab_python = None
  if not os.path.exists(mcp_path):
    errors.append(f"mcp_config.json missing at {mcp_path}")
  else:
    try:
      with open(mcp_path) as f:
        mcp_config = json.load(f)
      ztab_entry = mcp_config.get("mcpServers", {}).get("ztab")
      if not ztab_entry:
        errors.append("mcp_config.json has no 'ztab' under mcpServers")
      else:
        ztab_python = ztab_entry.get("command", "")
        if not ztab_python:
          errors.append("ztab entry has no 'command' field")
    except json.JSONDecodeError as e:
      errors.append(f"mcp_config.json is invalid JSON: {e}")

  # Check 2: backends.json exists with correct TEE
  backends_path = os.path.join(agent_home, ".ztab", "backends.json")
  if not os.path.exists(backends_path):
    errors.append(f"backends.json missing at {backends_path}")
  else:
    try:
      with open(backends_path) as f:
        backends = json.load(f)
      found = False
      for b in backends.get("backends", []):
        if (b.get("host") == expected_host
            and b.get("port") == expected_port):
          found = True
          # Check verifier if specified
          if expected_verifier:
            actual_verifier = b.get("verifier", "")
            if actual_verifier != expected_verifier:
              errors.append(
                  f"backends.json verifier mismatch: "
                  f"expected={expected_verifier} got={actual_verifier}"
              )
          break
      if not found:
        errors.append(
            f"backends.json has no entry matching "
            f"host={expected_host} port={expected_port}"
        )
    except json.JSONDecodeError as e:
      errors.append(f"backends.json is invalid JSON: {e}")

  # Check 3: virtualenv exists at path specified in mcp_config.json
  if ztab_python:
    if not os.path.isfile(ztab_python):
      errors.append(
          f"Python binary from mcp_config.json not found: {ztab_python}"
      )
  elif not errors:
    errors.append("Could not determine venv path (no ztab command in config)")

  return (len(errors) == 0, errors)


def _wait_for_phase1_completion(ports, csrf_token, cascade_ids, run_dir,
                                expected_host, expected_port,
                                expected_verifier=None,
                                timeout_secs=300):
  """Poll all agents until Phase 1 install is verified.

  Checks both agent status (IDLE) and disk state. If the agent is still
  running but the install artifacts are already on disk, we accept it
  (the agent may be generating a confirmation message after install).

  Returns True if all agents completed and passed validation.
  """
  start = time.time()
  completed = set()
  num_agents = len(ports)
  last_log = {}  # Track last logged status per agent
  idle_incomplete_since = {}  # Track per-agent IDLE+incomplete grace
  IDLE_INCOMPLETE_GRACE_SECS = 30  # Grace period for background pip install

  while time.time() - start < timeout_secs:
    for i, (port, cid) in enumerate(zip(ports, cascade_ids), 1):
      if i in completed:
        continue
      info = get_agent_status(port, csrf_token, cid)
      elapsed = int(time.time() - start)
      status_name = info["status_name"]
      num_steps = info["num_steps"]

      # Log status changes and periodic heartbeats
      prev = last_log.get(i)
      if prev != (status_name, num_steps) or elapsed % 30 == 0:
        print(f"  [{elapsed}s] Agent {i}: status={status_name} "
              f"steps={num_steps}", file=sys.stderr)
        last_log[i] = (status_name, num_steps)

      # Check disk state regardless of agent status — the install
      # may be done while the agent is still generating its summary
      if num_steps > 0:
        ok, errs = _verify_installation(
            run_dir, i, expected_host, expected_port,
            expected_verifier=expected_verifier
        )
        if ok:
          if info["status"] == 1:  # IDLE — clean completion
            print(f"  Agent {i}: Phase 1 PASS ✓ (IDLE after {elapsed}s)")
            completed.add(i)
            idle_incomplete_since.pop(i, None)
        elif info["status"] == 1:  # IDLE but install incomplete
          if i not in idle_incomplete_since:
            idle_incomplete_since[i] = time.time()
            print(f"  [{elapsed}s] Agent {i}: IDLE but install incomplete "
                  f"— waiting for background tasks "
                  f"({IDLE_INCOMPLETE_GRACE_SECS}s grace)",
                  file=sys.stderr)
          elif time.time() - idle_incomplete_since[i] > IDLE_INCOMPLETE_GRACE_SECS:
            print(f"  Agent {i}: Phase 1 FAIL ✘ (IDLE but install "
                  f"incomplete after {IDLE_INCOMPLETE_GRACE_SECS}s grace)")
            for err in errs:
              print(f"    - {err}", file=sys.stderr)
            return False
        else:
          idle_incomplete_since.pop(i, None)

      if info["status"] == -1:  # ERROR
        print(f"  Agent {i}: Phase 1 ERROR: {info['error']}",
              file=sys.stderr)
        return False

    if len(completed) == num_agents:
      return True
    time.sleep(5)

  elapsed = int(time.time() - start)
  print(f"Phase 1 timeout after {elapsed}s. Completed: {completed}",
        file=sys.stderr)
  return False


if __name__ == "__main__":
  main()

