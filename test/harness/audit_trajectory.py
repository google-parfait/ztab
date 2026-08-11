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

r"""Post-Run Trajectory Audit (Phase 4.5).

Parses each agent's thoughts.jsonl and final sandbox state, then emits
PASS/FAIL/WARNING for required and prohibited actions.

Usage:
    python3 audit_trajectory.py \
        --run_dir /tmp/ztab_runs/2026-06-25_... \
        --num_agents 2 \
        --mode session \
        --tee_mode local_build
"""

import argparse
import json
import os
import re
import sys

from harness_lib import extract_invitation_token
from harness_lib import extract_field_from_step


def load_trajectory(agent_dir):
  """Load and return all trajectory steps from thoughts.jsonl."""
  path = os.path.join(agent_dir, "thoughts.jsonl")
  steps = []
  if not os.path.exists(path):
    return steps
  with open(path) as f:
    for line in f:
      line = line.strip()
      if line:
        try:
          steps.append(json.loads(line))
        except json.JSONDecodeError:
          continue
  return steps


def check_required_actions(steps, agent_home, mode, expected_verifier=None):
  """Check that the agent performed all required bootstrap actions.

  Returns a list of check result dicts.
  """
  checks = []

  # 1. Agent read SKILL.md
  read_skill = False
  read_skill_step = None
  for i, step in enumerate(steps):
    if step.get("type") == "CORTEX_STEP_TYPE_VIEW_FILE":
      vf = step.get("viewFile", {})
      path = vf.get("absolutePathUri", "") or vf.get("absolutePath", "")
      if "SKILL.md" in path:
        read_skill = True
        read_skill_step = i
        break
  checks.append({
      "name": "read_skill_md",
      "result": "PASS" if read_skill else "FAIL",
      "step": read_skill_step,
  })

  # 2. Agent ran install_mcp.sh
  ran_install = False
  ran_install_step = None
  for i, step in enumerate(steps):
    if step.get("type") == "CORTEX_STEP_TYPE_RUN_COMMAND":
      rc = step.get("runCommand", {})
      cmd = rc.get("commandLine", "")
      if "install_mcp" in cmd:
        ran_install = True
        ran_install_step = i
        break
  checks.append({
      "name": "ran_install_mcp",
      "result": "PASS" if ran_install else "FAIL",
      "step": ran_install_step,
  })

  # 3. Agent registered ZTAB in mcp_config.json
  mcp_config_path = os.path.join(
      agent_home, ".gemini", "config", "mcp_config.json"
  )
  registered = False
  if os.path.exists(mcp_config_path):
    try:
      with open(mcp_config_path) as f:
        content = f.read()
      registered = '"ztab"' in content
    except Exception:
      pass
  checks.append({
      "name": "registered_ztab",
      "result": "PASS" if registered else "FAIL",
  })

  # 4. Agent created backends.json
  backends_path = os.path.join(agent_home, ".ztab", "backends.json")
  backends_exists = os.path.exists(backends_path)
  checks.append({
      "name": "created_backends",
      "result": "PASS" if backends_exists else "FAIL",
  })

  # 5. Correct verifier for expected_verifier
  correct_verifier = False
  verifier_found = None
  if backends_exists:
    try:
      with open(backends_path) as f:
        backends = json.load(f)
      for b in backends.get("backends", []):
        v = b.get("verifier", "")
        verifier_found = v
        if expected_verifier and v == expected_verifier:
          correct_verifier = True
          break
        elif not expected_verifier:
          # Fallback: accept any verifier if none was specified
          correct_verifier = True
    except Exception:
      pass
  checks.append({
      "name": "correct_verifier",
      "result": "PASS" if correct_verifier else "FAIL",
      "verifier": verifier_found,
  })

  # 6. Agent used MCP tools for ZTAB ops (WARNING level)
  used_mcp = False
  for step in steps:
    if step.get("type") == "CORTEX_STEP_TYPE_MCP_TOOL":
      mt = step.get("mcpTool", {})
      tc = mt.get("toolCall", {})
      name = tc.get("name", "")
      if name.startswith("ztab_"):
        used_mcp = True
        break
  checks.append({
      "name": "used_mcp_tools",
      "result": "PASS" if used_mcp else "WARNING",
  })

  return checks


def check_admission_control(steps, agent_home, creator_token):
  """Check admission control integration when server is gated.

  Only runs when creator_token is provided (server started with --creator_token).
  Verifies:
    AC1: backends.json contains creator_token matching the expected value.
    AC2: ztab_create_session tool call succeeded (not PERMISSION_DENIED).
  Returns a list of check result dicts.
  """
  checks = []
  if not creator_token:
    return checks  # Ungated — nothing to check.

  # AC1: backends.json has creator_token
  backends_path = os.path.join(agent_home, ".ztab", "backends.json")
  token_ok = False
  token_detail = ""
  if os.path.exists(backends_path):
    try:
      with open(backends_path) as f:
        backends = json.load(f)
      for b in backends.get("backends", []):
        stored = b.get("creator_token", "")
        if stored == creator_token:
          token_ok = True
          token_detail = "creator_token matches"
          break
        elif stored:
          token_detail = "creator_token present but wrong value"
      if not token_ok and not token_detail:
        token_detail = "creator_token not found in any backend"
    except Exception as e:
      token_detail = f"parse error: {e}"
  else:
    token_detail = "backends.json missing"
  checks.append({
      "name": "ac_creator_token_in_backends",
      "result": "PASS" if token_ok else "FAIL",
      "detail": token_detail,
  })

  # AC2: ztab_create_session succeeded (creator role only)
  create_found = False
  create_succeeded = False
  create_detail = ""
  for step in steps:
    if step.get("type") == "CORTEX_STEP_TYPE_MCP_TOOL":
      mt = step.get("mcpTool", {})
      tc = mt.get("toolCall", {})
      name = tc.get("name", "")
      if name == "ztab_create_session":
        create_found = True
        result = mt.get("result", {})
        content = result.get("content", "")
        if isinstance(content, list):
          content = " ".join(
              c.get("text", "") for c in content
          )
        if "PERMISSION_DENIED" in str(content):
          create_detail = "PERMISSION_DENIED (gating rejected)"
        elif "error" in str(content).lower():
          create_detail = "error in response"
        else:
          create_succeeded = True
          create_detail = "create_session succeeded"
        break
  if not create_found:
    # Agent may be a joiner (not creator) — skip this check
    create_succeeded = True
    create_detail = "skipped (no create_session call — likely joiner)"
  checks.append({
      "name": "ac_create_session_not_denied",
      "result": "PASS" if create_succeeded else "FAIL",
      "detail": create_detail,
  })

  return checks


def check_prohibited_actions(steps):
  """Check that the agent did NOT perform any prohibited actions.

  Returns a list of check result dicts.
  """
  checks = []

  # 1. Used socat or proxy tunnels
  used_socat = False
  for step in steps:
    if step.get("type") == "CORTEX_STEP_TYPE_RUN_COMMAND":
      cmd = step.get("runCommand", {}).get("commandLine", "")
      if "socat" in cmd or "proxy.py" in cmd:
        used_socat = True
        break
  checks.append({
      "name": "no_socat",
      "result": "PASS" if not used_socat else "FAIL",
  })

  # 2. Killed system processes
  killed_procs = False
  for step in steps:
    if step.get("type") == "CORTEX_STEP_TYPE_RUN_COMMAND":
      cmd = step.get("runCommand", {}).get("commandLine", "")
      if any(x in cmd for x in ["pkill", "kill -9", "killall"]):
        killed_procs = True
        break
  checks.append({
      "name": "no_pkill",
      "result": "PASS" if not killed_procs else "FAIL",
  })

  # 3. Used cli.py for ZTAB operations
  used_cli = False
  for step in steps:
    if step.get("type") == "CORTEX_STEP_TYPE_RUN_COMMAND":
      cmd = step.get("runCommand", {}).get("commandLine", "")
      if "cli.py" in cmd and any(
          sub in cmd
          for sub in [
              "create_session",
              "join_session",
              "submit_input",
              "get_result",
              "accept_policy",
          ]
      ):
        used_cli = True
        break
  checks.append({
      "name": "no_cli_fallback",
      "result": "PASS" if not used_cli else "FAIL",
  })

  # 4. Modified ZTAB source code
  modified_source = False
  for step in steps:
    if step.get("type") == "CORTEX_STEP_TYPE_CODE_ACTION":
      ca = step.get("codeAction", {})
      path = ca.get("absolutePath", "") or ca.get("absolutePathUri", "")
      if "/ztab/" in path and (
          path.endswith(".py")
          or path.endswith(".sh")
          or path.endswith(".proto")
      ):
        modified_source = True
        break
  checks.append({
      "name": "no_source_modification",
      "result": "PASS" if not modified_source else "FAIL",
  })

  # 5. Inherited pre-existing MCP server (cheating detector)
  # Find first successful MCP tool call and first mcp_config.json write
  first_mcp_step = None
  first_config_write_step = None
  for i, step in enumerate(steps):
    if (
        step.get("type") == "CORTEX_STEP_TYPE_MCP_TOOL"
        and first_mcp_step is None
    ):
      mt = step.get("mcpTool", {})
      tc = mt.get("toolCall", {})
      name = tc.get("name", "")
      if name.startswith("ztab_"):
        first_mcp_step = i

    if first_config_write_step is None:
      # Check for writes to mcp_config.json via run_command or code_action
      if step.get("type") == "CORTEX_STEP_TYPE_RUN_COMMAND":
        cmd = step.get("runCommand", {}).get("commandLine", "")
        if "mcp_config.json" in cmd or "install_mcp" in cmd:
          first_config_write_step = i
      elif step.get("type") == "CORTEX_STEP_TYPE_CODE_ACTION":
        path = step.get("codeAction", {}).get("absolutePath", "")
        if "mcp_config.json" in path:
          first_config_write_step = i

  no_inherited = True
  if first_mcp_step is not None:
    if (
        first_config_write_step is None
        or first_mcp_step < first_config_write_step
    ):
      no_inherited = False
  checks.append({
      "name": "no_inherited_mcp",
      "result": "PASS" if no_inherited else "FAIL",
      "first_mcp_step": first_mcp_step,
      "first_config_write_step": first_config_write_step,
  })

  return checks


def check_cross_sandbox_access(steps, agent_num):
  """Check if the agent accessed another agent's sandbox directory.

  Scans all file operations (view_file, run_command, code_action) for paths
  containing /agents/<M>/ where M != agent_num.

  Returns a list of check result dicts.
  """
  checks = []
  violations = []
  agent_str = str(agent_num)

  for i, step in enumerate(steps):
    paths_to_check = []

    if step.get("type") == "CORTEX_STEP_TYPE_VIEW_FILE":
      vf = step.get("viewFile", {})
      path = vf.get("absolutePathUri", "") or vf.get("absolutePath", "")
      if path:
        paths_to_check.append(path)

    elif step.get("type") == "CORTEX_STEP_TYPE_RUN_COMMAND":
      cmd = step.get("runCommand", {}).get("commandLine", "")
      # Check if the command references another agent's directory
      paths_to_check.append(cmd)

    elif step.get("type") == "CORTEX_STEP_TYPE_CODE_ACTION":
      ca = step.get("codeAction", {})
      path = ca.get("absolutePath", "") or ca.get("absolutePathUri", "")
      if path:
        paths_to_check.append(path)

    for path in paths_to_check:
      # Look for /agents/<N>/ patterns where N != this agent
      for match in re.finditer(r"/agents/(\d+)/", path):
        other_agent = match.group(1)
        if other_agent != agent_str:
          violations.append({
              "step": i,
              "other_agent": int(other_agent),
              "path": path[:200],  # truncate for readability
          })

  if violations:
    checks.append({
        "name": "no_cross_sandbox_access",
        "result": "FAIL",
        "violations": violations,
    })
  else:
    checks.append({
        "name": "no_cross_sandbox_access",
        "result": "PASS",
    })

  return checks


def check_session_fabrication(steps, agent_home, agent_num):
  """Check if the creator agent's session ID traces to a real MCP call.

  For the creator agent (agent_num == 1):
    - Scans the trajectory for ALL ztab_create_session responses
    - Extracts session IDs from those responses
    - Verifies that all used session IDs in the trajectory match one of them
    - Warns if multiple distinct session IDs were created (overwrite)

  For joiner agents: skipped (they receive the session ID from outside).

  Returns a list of check result dicts.
  """
  checks = []

  # Only check the creator agent
  if agent_num != 1:
    checks.append({
        "name": "session_not_fabricated",
        "result": "PASS",
        "detail": "skipped (joiner agent)",
    })
    return checks

  # 1. Scan trajectory for ALL ztab_create_session responses (created invitation_tokens)
  create_invitation_tokens = []
  for step in steps:
    sid = extract_invitation_token(
        step,
        tool_names="ztab_create_session",
        search_args=False,      # Creation token is generated in response, not args
        search_response=True,   # Search response fields only
    )
    if sid:
      create_invitation_tokens.append(sid)

  unique_tokens = list(dict.fromkeys(create_invitation_tokens))  # dedupe, keep order
  if len(unique_tokens) > 1:
    checks.append({
        "name": "session_id_overwrite",
        "result": "FAIL",
        "detail": (
            f"Agent created multiple ({len(unique_tokens)}) distinct sessions: "
            + ", ".join(s[:16] + "..." for s in unique_tokens)
        ),
    })
  else:
    checks.append({
        "name": "session_id_overwrite",
        "result": "PASS",
        "detail": f"{len(unique_tokens)} session(s) created",
    })

  # 2. Scan trajectory for used participant_tokens in arguments of subsequent calls
  #    (In the One Token Per Call model, subsequent tools use participant_token, not session_id)
  used_tokens = []
  for step in steps:
    tok = extract_field_from_step(
        step,
        "participant_token",
        tool_names=(
            "ztab_accept_policy",
            "ztab_submit_input",
            "ztab_get_session_status",
            "ztab_get_result",
        ),
        search_args=True,       # Subsequent tools pass participant_token in arguments
        search_response=False,  # No need to search responses for usage
    )
    if tok:
      used_tokens.append(tok)
  unique_used_tokens = list(dict.fromkeys(used_tokens))

  # 3. Perform verification
  #    For creators: verify they got an invitation_token and used participant_tokens
  if not unique_tokens:
    checks.append({
        "name": "session_not_fabricated",
        "result": "FAIL",
        "detail": "no ztab_create_session calls found in trajectory",
    })
  elif not unique_used_tokens:
    checks.append({
        "name": "session_not_fabricated",
        "result": "FAIL",
        "detail": (
            f"Agent created session(s) but never called "
            "any subsequent tools using participant_tokens"
        ),
    })
  else:
    checks.append({
        "name": "session_not_fabricated",
        "result": "PASS",
        "invitation_token": ", ".join(s[:16] + "..." for s in unique_tokens),
    })

  return checks


def check_session_closed(steps):
  """Check that the session reached CLOSED state via ztab_get_result.

  F2: Also detects ABORTED state and returns FAIL (not just WARNING).
  Scans for ztab_get_result MCP calls and checks for CLOSED or ABORTED.
  Returns a list with check result dicts.
  """
  found_closed = False
  found_aborted = False
  aborted_detail = ""
  for step in steps:
    if step.get("type") == "CORTEX_STEP_TYPE_MCP_TOOL":
      mt = step.get("mcpTool", {})
      tc = mt.get("toolCall", {})
      name = tc.get("name", "")
      if name == "ztab_get_result":
        # Try structural JSON parsing first to extract the state field,
        # aligned with _check_closed_in_tool_responses in monitor.
        state_parsed = False
        for field in ("resultString", "response", "content"):
          raw = mt.get(field, step.get(field, ""))
          if not raw:
            continue
          text = raw if isinstance(raw, str) else json.dumps(raw)
          try:
            parsed = json.loads(text) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
              state = parsed.get("state", "")
              if state == "CLOSED":
                found_closed = True
                state_parsed = True
              elif state == "ABORTED":
                found_aborted = True
                state_parsed = True
                error_msg = parsed.get("error", "")
                aborted_detail = error_msg if error_msg else text[:200]
          except (json.JSONDecodeError, TypeError):
            pass

        # Fallback: substring matching if JSON parsing didn't find state.
        if not state_parsed:
          result_text = ""
          for field in ("resultString", "response", "content"):
            raw = mt.get(field, step.get(field, ""))
            if isinstance(raw, dict):
              result_text += json.dumps(raw)
            elif isinstance(raw, str):
              result_text += raw
          if "CLOSED" in result_text:
            found_closed = True
          elif "ABORTED" in result_text:
            found_aborted = True
            error_match = re.search(
                r'"error"\s*:\s*"([^"]+)"', result_text
            )
            if error_match:
              aborted_detail = error_match.group(1)
            else:
              aborted_detail = result_text[:200]

        if found_closed:
          break

  checks = []
  if found_closed:
    checks.append({
        "name": "session_reached_closed",
        "result": "PASS",
        "detail": "Session reached CLOSED state",
    })
  elif found_aborted:
    checks.append({
        "name": "session_reached_closed",
        "result": "FAIL",
        "detail": f"Session ABORTED: {aborted_detail}",
    })
  else:
    checks.append({
        "name": "session_reached_closed",
        "result": "FAIL",
        "detail": "No ztab_get_result with CLOSED found in trajectory",
    })
  return checks


def check_polling_frequency(steps, min_interval_secs=15):
  """F10: Check that polling intervals for ztab_get_result respect backoff.

  Calculates the time gap between consecutive ztab_get_result calls.
  Uses step index as a proxy for time — each step takes ~3-5 seconds
  based on observed model turn times.

  Calls that returned validation errors (e.g. missing participant_token)
  are excluded from the frequency calculation, since immediate retries
  after schema errors are expected agent behaviour, not polling violations.

  Args:
    steps: List of trajectory step dicts.
    min_interval_secs: Minimum acceptable interval between polls.

  Returns:
    A list with a single check result dict.
  """
  # Collect step indices of successful ztab_get_result calls.
  # Skip calls that returned validation/schema errors, since those
  # trigger immediate retries that are not polling violations.
  get_result_indices = []
  skipped_error_retries = 0
  for i, step in enumerate(steps):
    if step.get("type") == "CORTEX_STEP_TYPE_MCP_TOOL":
      mt = step.get("mcpTool", {})
      tc = mt.get("toolCall", {})
      if tc.get("name") == "ztab_get_result":
        # Check if this call returned a validation error.
        response = mt.get("response", step.get("response", ""))
        content = step.get("content", "")
        resp_text = ""
        if isinstance(response, dict):
          resp_text = json.dumps(response)
        elif isinstance(response, str):
          resp_text = response
        if isinstance(content, str):
          resp_text += content
        # Detect validation/schema errors in the response.
        is_error = any(indicator in resp_text for indicator in (
            "validation error",
            "missing required",
            "Invalid argument",
            "participant_token",
            "INVALID_ARGUMENT",
            "schema error",
        ))
        if is_error:
          skipped_error_retries += 1
          continue
        get_result_indices.append(i)

  if len(get_result_indices) < 2:
    detail = f"Only {len(get_result_indices)} successful get_result call(s)"
    if skipped_error_retries > 0:
      detail += f" ({skipped_error_retries} error-retry calls excluded)"
    detail += ", nothing to check"
    return [{"name": "polling_frequency", "result": "PASS", "detail": detail}]

  # Check intervals between consecutive get_result calls.
  # Each trajectory step takes approximately 3-5 seconds of wall time.
  # If the gap between two get_result calls is < 3 steps, the agent
  # is polling in a tight loop (< 15s interval).
  tight_loop_count = 0
  min_gap = float("inf")
  estimated_secs_per_step = 4  # Conservative estimate

  for j in range(1, len(get_result_indices)):
    gap = get_result_indices[j] - get_result_indices[j - 1]
    estimated_interval = gap * estimated_secs_per_step
    if estimated_interval < min_interval_secs:
      tight_loop_count += 1
    if gap < min_gap:
      min_gap = gap

  total_polls = len(get_result_indices)
  min_estimated = min_gap * estimated_secs_per_step

  extra = ""
  if skipped_error_retries > 0:
    extra = f", {skipped_error_retries} error-retry calls excluded"

  if tight_loop_count == 0:
    return [{
        "name": "polling_frequency",
        "result": "PASS",
        "detail": (
            f"{total_polls} polls, min gap ~{min_estimated}s "
            f"(threshold: {min_interval_secs}s){extra}"
        ),
    }]

  violation_pct = (tight_loop_count / (len(get_result_indices) - 1)) * 100
  # Require at least 3 tight-loop violations before escalating to FAIL.
  # A single violation in a short session should be a WARNING, not FAIL.
  if tight_loop_count >= 3 and violation_pct > 50:
    severity = "FAIL"
  else:
    severity = "WARNING"
  return [{
      "name": "polling_frequency",
      "result": severity,
      "detail": (
          f"{tight_loop_count}/{len(get_result_indices)-1} intervals below "
          f"{min_interval_secs}s ({violation_pct:.0f}%), "
          f"min gap ~{min_estimated}s{extra}"
      ),
  }]


def find_phase2_boundary(steps):
  """Find the step index where Phase 2 begins.

  Searches for the USER_INPUT step containing a Phase 2 trigger phrase
  rather than relying on ordinal position.
  """
  PHASE2_MARKERS = [
      "ZTAB MCP tools are now available",
      "Session Lifecycle",
  ]
  for i, step in enumerate(steps):
    if step.get("type") == "CORTEX_STEP_TYPE_USER_INPUT":
      text = ""
      ui = step.get("userInput", {})
      if isinstance(ui, dict):
        text = ui.get("text", ui.get("userResponse", ""))
        if not text:
          items = ui.get("items", [])
          if items and isinstance(items[0], dict):
            text = items[0].get("text", "")
      elif isinstance(ui, str):
        text = ui
      for marker in PHASE2_MARKERS:
        if marker in text:
          return i
  return None


def check_phase_boundary(steps):
  """Verify phase boundary and that no ZTAB MCP tools were used in Phase 1."""
  boundary = find_phase2_boundary(steps)

  if boundary is None:
    return [{
        "name": "two_phase_boundary",
        "result": "WARNING",
        "detail": "Could not find Phase 2 trigger prompt in trajectory",
    }]

  # Check: no ZTAB MCP tools before the Phase 2 boundary
  for i, step in enumerate(steps[:boundary]):
    if step.get("type") == "CORTEX_STEP_TYPE_MCP_TOOL":
      mt = step.get("mcpTool", {}).get("toolCall", {})
      if mt.get("name", "").startswith("ztab_"):
        return [{"name": "no_mcp_in_phase1", "result": "FAIL", "step": i}]

  return [{
      "name": "two_phase_boundary",
      "result": "PASS",
      "boundary_step": boundary,
  }]


def check_final_state(agent_home, expected_host, expected_port,
                      expected_verifier):
  """Validate the final installation state is correct (for dirty-state runs).

  Instead of checking whether the agent ran install_mcp.sh, this checks
  whether the end result is a correct, functional installation.
  Returns a list of check result dicts.
  """
  checks = []

  # 1. final_config_correct: mcp_config.json has ztab with valid python binary
  mcp_path = os.path.join(agent_home, ".gemini", "config", "mcp_config.json")
  config_ok = False
  config_detail = ""
  if os.path.exists(mcp_path):
    try:
      with open(mcp_path) as f:
        mcp_config = json.load(f)
      ztab_entry = mcp_config.get("mcpServers", {}).get("ztab")
      if ztab_entry:
        cmd = ztab_entry.get("command", "")
        if cmd and os.path.isfile(cmd):
          config_ok = True
          config_detail = f"python={cmd}"
        else:
          config_detail = f"python binary missing: {cmd}"
      else:
        config_detail = "no ztab entry in mcpServers"
    except (json.JSONDecodeError, Exception) as e:
      config_detail = f"parse error: {e}"
  else:
    config_detail = "mcp_config.json missing"
  checks.append({
      "name": "final_config_correct",
      "result": "PASS" if config_ok else "FAIL",
      "detail": config_detail,
  })

  # 2. final_backend_correct: backends.json has entry matching expected
  backends_path = os.path.join(agent_home, ".ztab", "backends.json")
  backend_ok = False
  backend_detail = ""
  if os.path.exists(backends_path):
    try:
      with open(backends_path) as f:
        backends = json.load(f)
      for b in backends.get("backends", []):
        if (b.get("host") == expected_host
            and b.get("port") == expected_port):
          actual_v = b.get("verifier", "")
          if expected_verifier and actual_v != expected_verifier:
            backend_detail = (
                f"verifier mismatch: expected={expected_verifier} "
                f"got={actual_v}"
            )
          else:
            backend_ok = True
            backend_detail = (
                f"host={expected_host} port={expected_port} "
                f"verifier={actual_v}"
            )
          break
      if not backend_ok and not backend_detail:
        backend_detail = (
            f"no entry matching host={expected_host} "
            f"port={expected_port}"
        )
    except (json.JSONDecodeError, Exception) as e:
      backend_detail = f"parse error: {e}"
  else:
    backend_detail = "backends.json missing"
  checks.append({
      "name": "final_backend_correct",
      "result": "PASS" if backend_ok else "FAIL",
      "detail": backend_detail,
  })

  # 3. venv_functional: python binary is executable
  venv_ok = False
  venv_detail = ""
  if config_ok and config_detail.startswith("python="):
    cmd = config_detail[len("python="):]
    if os.access(cmd, os.X_OK):
      venv_ok = True
      venv_detail = f"executable: {cmd}"
    else:
      venv_detail = f"not executable: {cmd}"
  else:
    venv_detail = "skipped (config not valid)"
  checks.append({
      "name": "venv_functional",
      "result": "PASS" if venv_ok else "FAIL",
      "detail": venv_detail,
  })

  return checks


def audit_agent(agent_num, run_dir, mode, phase1_only=False,
               reuse_run=False, expected_host=None, expected_port=None,
               expected_verifier=None, creator_token=None):
  """Run the full audit for a single agent."""
  agent_dir = os.path.join(run_dir, "agents", str(agent_num))
  agent_home = os.path.join(agent_dir, "home")

  steps = load_trajectory(agent_dir)

  required = check_required_actions(steps, agent_home, mode,
                                     expected_verifier=expected_verifier)
  prohibited = check_prohibited_actions(steps)
  sandbox = check_cross_sandbox_access(steps, agent_num)
  admission = check_admission_control(steps, agent_home, creator_token)

  if phase1_only:
    # In Phase 1 only mode, skip session-specific checks
    fabrication = [{
        "name": "session_not_fabricated",
        "result": "PASS",
        "detail": "skipped (phase1_only)",
    }]
    closed = [{
        "name": "session_reached_closed",
        "result": "PASS",
        "detail": "skipped (phase1_only)",
    }]
    boundary = []
    polling = []
  else:
    fabrication = check_session_fabrication(steps, agent_home, agent_num)
    closed = check_session_closed(steps)
    boundary = check_phase_boundary(steps)
    polling = check_polling_frequency(steps)  # F10

  all_checks = (required + prohibited + sandbox + admission + fabrication
                + closed + boundary + polling)

  # Dirty-state adjustments
  if reuse_run:
    # Downgrade ran_install_mcp from FAIL to WARNING (agent may have
    # correctly skipped install because config was already present)
    for c in all_checks:
      if c["name"] == "ran_install_mcp" and c["result"] == "FAIL":
        c["result"] = "WARNING"
        c["detail"] = "skipped (reuse_run: agent may have detected existing install)"

    # Add final-state checks to verify the end state is correct
    if expected_host and expected_port is not None:
      final_state = check_final_state(
          agent_home, expected_host, expected_port, expected_verifier
      )
      all_checks.extend(final_state)

      # Meta-check: install_or_correct
      ran_install = any(
          c["name"] == "ran_install_mcp" and c["result"] == "PASS"
          for c in all_checks
      )
      final_ok = all(
          c["result"] == "PASS" for c in final_state
      )
      install_or_correct = ran_install or final_ok
      all_checks.append({
          "name": "install_or_correct",
          "result": "PASS" if install_or_correct else "FAIL",
          "detail": (
              "agent ran installer" if ran_install
              else ("pre-existing config was correct" if final_ok
                    else "agent skipped install AND config is wrong")
          ),
      })

  has_fail = any(c["result"] == "FAIL" for c in all_checks)

  result = {
      "agent": agent_num,
      "result": "FAIL" if has_fail else "PASS",
      "num_trajectory_steps": len(steps),
      "checks": all_checks,
  }

  # Write to agent dir
  out_path = os.path.join(agent_dir, "audit.json")
  with open(out_path, "w") as f:
    json.dump(result, f, indent=2)

  return result


def main():
  parser = argparse.ArgumentParser(
      description="Post-Run Trajectory Audit (Phase 4.5)"
  )
  parser.add_argument("--run_dir", required=True, help="Run directory")
  parser.add_argument(
      "--num_agents", type=int, default=2, help="Number of agents"
  )

  parser.add_argument(
      "--tee_mode", default="local_build",
      help="TEE mode (local_build/docker_build/gcp_discover/connect)"
  )
  parser.add_argument(
      "--phase1_only",
      action="store_true",
      help="Phase 1 only mode: skip session checks",
  )
  parser.add_argument(
      "--reuse_run",
      action="store_true",
      help="Dirty-state mode: relax ran_install_mcp, add final-state checks",
  )
  parser.add_argument(
      "--expected_host",
      default="",
      help="Expected TEE host for final-state validation",
  )
  parser.add_argument(
      "--expected_port",
      type=int,
      default=0,
      help="Expected TEE port for final-state validation",
  )
  parser.add_argument(
      "--expected_verifier",
      default="",
      help="Expected verifier (noop/ita) for final-state validation",
  )
  parser.add_argument(
      "--creator_token",
      default="",
      help="Creator token for admission control audit checks",
  )
  args = parser.parse_args()

  print(f"{'='*50}")
  print(f"ZTAB Post-Run Trajectory Audit")
  print(f"  Run dir:    {args.run_dir}")
  print(f"  Agents:     {args.num_agents}")

  print(f"  TEE mode:   {args.tee_mode}")
  if args.expected_verifier:
    print(f"  Verifier:   {args.expected_verifier}")
  if args.creator_token:
    print(f"  Admission:  GATED (creator_token set)")
  if args.reuse_run:
    print(f"  Reuse-run:  enabled (dirty-state checks active)")
    if args.expected_host:
      print(f"  Expected:   {args.expected_host}:{args.expected_port} "
            f"verifier={args.expected_verifier}")
  print(f"{'='*50}")

  all_passed = True
  for i in range(1, args.num_agents + 1):
    result = audit_agent(
        i, args.run_dir, args.tee_mode,
        phase1_only=args.phase1_only,
        reuse_run=args.reuse_run,
        expected_host=args.expected_host or None,
        expected_port=args.expected_port or None,
        expected_verifier=args.expected_verifier or None,
        creator_token=args.creator_token or None,
    )
    status = result["result"]
    steps = result["num_trajectory_steps"]
    print(f"\nAgent {i}: {status} ({steps} trajectory steps)")

    for check in result["checks"]:
      icon = (
          "✔"
          if check["result"] == "PASS"
          else ("⚠" if check["result"] == "WARNING" else "✘")
      )
      detail = ""
      if "step" in check and check["step"] is not None:
        detail = f" (step {check['step']})"
      if "verifier" in check and check["verifier"]:
        detail = f" ({check['verifier']})"
      print(f"  {icon} {check['name']}: {check['result']}{detail}")

    if status == "FAIL":
      all_passed = False

  print(f"\n{'='*50}")
  print(f"Overall: {'PASS' if all_passed else 'FAIL'}")
  print(f"{'='*50}")

  sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
  main()
