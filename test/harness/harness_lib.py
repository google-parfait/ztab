"""Shared harness library for ZTAB test scripts.

B2: Extracted from duplicated code in trigger_session_test.py and
monitor_session_test.py. Provides LS API helpers, status enums,
trajectory step parsing, and timer detection.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.request


# ---------------------------------------------------------------------------
# Port Discovery (B.2)
# ---------------------------------------------------------------------------


def get_discovered_http_port(workspace_id, gemini_dir, timeout=15):
  """Discover the HTTP port of a running Language Server instance.

  The LS writes a discovery JSON file to:
    {gemini_dir}/daemon/ls_{hash}.json
  where {hash} is the first 16 hex chars of SHA-256(workspace_id).

  This function polls for that file, reads the httpPort, and verifies
  the LS process is still alive (PID liveness check).

  Args:
    workspace_id: The workspace ID string (e.g. "file:///path/to/workspace").
        Must include the file:// prefix.
    gemini_dir: Path to the .gemini directory (e.g. /root/.gemini/app_data_dir).
    timeout: Max seconds to wait for the discovery file (default: 15).

  Returns:
    The discovered HTTP port (int).

  Raises:
    TimeoutError: If the discovery file is not found within timeout.
    RuntimeError: If the LS process died before the port was discovered.
  """
  # Normalize workspace_id: ensure file:// prefix
  if not workspace_id.startswith("file://"):
    workspace_id = f"file://{workspace_id}"

  # Compute the discovery file hash (first 16 hex chars of SHA-256)
  ws_hash = hashlib.sha256(workspace_id.encode()).hexdigest()[:16]
  discovery_path = os.path.join(gemini_dir, "daemon", f"ls_{ws_hash}.json")

  start = time.time()
  while time.time() - start < timeout:
    if os.path.exists(discovery_path):
      try:
        with open(discovery_path) as f:
          data = json.load(f)
        port = data.get("httpPort")
        pid = data.get("pid")

        if port:
          # PID liveness check
          if pid:
            try:
              os.kill(int(pid), 0)  # Signal 0 = check if process exists
            except OSError:
              raise RuntimeError(
                  f"LS process (PID {pid}) is dead. Discovery file stale."
              )
          return int(port)
      except (json.JSONDecodeError, KeyError, ValueError):
        pass  # File may be partially written, retry
    time.sleep(0.5)

  raise TimeoutError(
      f"LS discovery file not found at {discovery_path} within {timeout}s"
  )


# ---------------------------------------------------------------------------
# LS API Helpers
# ---------------------------------------------------------------------------


CONNECT_SERVICE_PATH = "/exa.language_server_pb.LanguageServerService/"


def ls_request(port, csrf_token, rpc_name, body):
  """Make a Connect-protocol JSON request to the LS.

  Args:
    port: Language Server port number.
    csrf_token: CSRF token for LS authentication.
    rpc_name: Name of the LS RPC to call.
    body: Request body dict.

  Returns:
    Parsed JSON response dict.
  """
  url = f"http://localhost:{port}{CONNECT_SERVICE_PATH}{rpc_name}"
  data = json.dumps(body).encode("utf-8")
  req = urllib.request.Request(
      url,
      data=data,
      headers={
          "Content-Type": "application/json",
          "x-codeium-csrf-token": csrf_token,
      },
      method="POST",
  )
  try:
    with urllib.request.urlopen(req, timeout=30) as resp:
      return json.loads(resp.read().decode("utf-8"))
  except urllib.error.HTTPError as e:
    body_text = e.read().decode("utf-8", errors="replace")
    print(f"HTTP {e.code} from {rpc_name}: {body_text}", file=sys.stderr)
    raise


def _go_format(val):
  """Format values in Go %v map style (e.g. map[k:v] and [v1 v2])."""
  if isinstance(val, dict):
    parts = []
    for k in sorted(val.keys()):
      parts.append(f"{k}:{_go_format(val[k])}")
    return f"map[{' '.join(parts)}]"
  elif isinstance(val, list):
    parts = [_go_format(x) for x in val]
    return f"[{' '.join(parts)}]"
  elif isinstance(val, bool):
    return "true" if val else "false"
  elif val is None:
    return "<nil>"
  else:
    return str(val)


def _format_timestamp(ts_str):
  """Format proto RFC3339 timestamps to seconds-precision YYYY-MM-DDTHH:MM:SSZ."""
  if not ts_str or ts_str == "unset":
    return "unset"
  if "." in ts_str:
    main_part = ts_str.split(".")[0]
    if ts_str.endswith("Z"):
      return main_part + "Z"
    return main_part
  return ts_str


def _format_step_to_str(step, include_thoughts=True, include_tool_calls=True):
  """Format a single trajectory step JSON to Go printStep style."""
  metadata = step.get("metadata", {})
  step_id = metadata.get("sourceTrajectoryStepInfo", {}).get("stepIndex", 0)

  raw_type = step.get("type", "")
  step_type = raw_type.replace("CORTEX_STEP_TYPE_", "").lower()

  raw_status = step.get("status", "")
  status = raw_status.replace("CORTEX_STEP_STATUS_", "").lower()

  time_str = _format_timestamp(metadata.get("createdAt", "unset"))

  role = "unknown"
  content = ""
  thinking = ""
  tool_call = None
  error = ""

  if step.get("plannerResponse") is not None:
    pr = step["plannerResponse"]
    content = pr.get("modifiedResponse") or pr.get("response", "")
    thinking = pr.get("thinking", "")
    role = "assistant"
    # toolCall can be in metadata
    tc_proto = metadata.get("toolCall")
    if tc_proto and tc_proto.get("name"):
      args_raw = tc_proto.get("argumentsJson", "{}")
      try:
        args = json.loads(args_raw)
      except Exception:
        args = args_raw
      tool_call = {"name": tc_proto.get("name"), "args": args}

  elif step.get("userInput") is not None:
    ui = step["userInput"]
    parts = []
    for item in ui.get("items", []):
      if item.get("text"):
        parts.append(item["text"])
    content = "\n".join(parts)
    role = "user"

  elif step.get("runCommand") is not None:
    rc = step["runCommand"]
    content = rc.get("commandLine", "")
    role = "tool"

  elif step.get("commandStatus") is not None:
    cs = step["commandStatus"]
    content = cs.get("combined", "")
    role = "tool_result"

  elif step.get("viewFile") is not None:
    vf = step["viewFile"]
    content = vf.get("absolutePathUri", "")
    role = "tool"

  elif step.get("grepSearch") is not None:
    gs = step["grepSearch"]
    content = gs.get("query", "")
    role = "tool"

  elif step.get("listDirectory") is not None:
    ld = step["listDirectory"]
    content = ld.get("directoryPathUri", "")
    role = "tool"

  elif step.get("find") is not None:
    f = step["find"]
    content = f.get("pattern", "") or f.get("searchDirectory", "")
    role = "tool"

  elif step.get("readUrlContent") is not None:
    ru = step["readUrlContent"]
    content = ru.get("url", "")
    role = "tool"

  elif step.get("searchWeb") is not None:
    sw = step["searchWeb"]
    content = sw.get("query", "")
    role = "tool"

  elif step.get("codeSearch") is not None:
    cs = step["codeSearch"]
    content = cs.get("query", "")
    role = "tool"

  elif step.get("mcpTool") is not None:
    mt = step["mcpTool"]
    content = mt.get("resultString", "")
    role = "tool"
    tc_proto = mt.get("toolCall")
    if tc_proto:
      step_type = tc_proto.get("name", "")
      args_raw = tc_proto.get("argumentsJson", "{}")
      try:
        args = json.loads(args_raw)
      except Exception:
        args = args_raw
      tool_call = {"name": tc_proto.get("name"), "args": args}

  elif step.get("errorMessage") is not None:
    em = step["errorMessage"]
    content = em.get("error", {}).get("shortError", "")
    error = content
    role = "system"

  elif step.get("ephemeralMessage") is not None:
    em = step["ephemeralMessage"]
    content = em.get("content", "")
    role = "system"

  elif step.get("finish") is not None:
    role = "system"

  # Visibility check (shouldShowStep filter)
  has_content = bool(content)
  has_error = bool(error)
  has_thinking = bool(thinking) and include_thoughts
  has_tool_call = (tool_call is not None) and include_tool_calls

  if not (has_content or has_error or has_thinking or has_tool_call):
    return ""

  out = []
  out.append("============================================================\n")
  out.append(
      f"Step {step_id}: {step_type} (Role: {role}, Status: {status}, Time:"
      f" {time_str})\n"
  )

  if thinking and include_thoughts:
    out.append(f"[THINKING]\n{thinking}\n")
  if tool_call and include_tool_calls:
    args_str = _go_format(tool_call["args"])
    out.append(f"[TOOL CALL] {tool_call['name']}({args_str})\n")
  if error:
    out.append(f"[ERROR] {error}\n")
  if content:
    out.append(f"{content}\n")

  return "".join(out)


def download_conversation(port, csrf_token, cascade_id, filepath):
  """Download the full agent conversation trajectory and write it in Go format.

  Replaces the CLI-based conversation download to run natively via Connect HTTP.
  """
  all_steps = []
  offset = 0
  while True:
    try:
      resp = ls_request(
          port,
          csrf_token,
          "GetCascadeTrajectorySteps",
          {
              "cascade_id": cascade_id,
              "step_offset": offset,
              "trajectory_verbosity": 1,  # DEBUG
          },
      )
    except Exception as e:
      print(f"Error fetching steps at offset {offset}: {e}", file=sys.stderr)
      raise

    steps = resp.get("steps", [])
    if not steps:
      break
    all_steps.extend(steps)
    offset += len(steps)

  with open(filepath, "w") as f:
    for step in all_steps:
      formatted = _format_step_to_str(
          step, include_thoughts=True, include_tool_calls=True
      )
      f.write(formatted)


# ---------------------------------------------------------------------------
# Status Enums
# ---------------------------------------------------------------------------

# CascadeRunStatus enum values from cortex_pb.
STATUS_NAMES = {
    0: "UNSPECIFIED",
    1: "IDLE",
    2: "GENERATING",
    3: "WAITING_FOR_USER",
    4: "EXECUTING_TOOL",
    5: "SUBMITTING_CODE_REVIEW",
    6: "CANCELING",
    7: "BUSY",
}

# The LS JSON API returns enum values as string names, not integers.
# This map normalizes them back to ints for the done-detection logic.
STATUS_STR_TO_INT = {
    "CASCADE_RUN_STATUS_UNSPECIFIED": 0,
    "CASCADE_RUN_STATUS_IDLE": 1,
    "CASCADE_RUN_STATUS_RUNNING": 2,
    "CASCADE_RUN_STATUS_WAITING_FOR_USER": 3,
    "CASCADE_RUN_STATUS_EXECUTING_TOOL": 4,
    "CASCADE_RUN_STATUS_SUBMITTING_CODE_REVIEW": 5,
    "CASCADE_RUN_STATUS_CANCELING": 6,
    "CASCADE_RUN_STATUS_BUSY": 7,
}


# ---------------------------------------------------------------------------
# Agent Status Polling
# ---------------------------------------------------------------------------


def get_agent_status(port, csrf_token, cascade_id):
  """Poll the LS for an agent's current status and step count.

  Args:
    port: Language Server port number.
    csrf_token: CSRF token for LS authentication.
    cascade_id: Agent cascade ID.

  Returns:
    Dict with: status (int), status_name (str), num_steps (int), error (str
    or None).
  """
  try:
    resp = ls_request(
        port,
        csrf_token,
        "GetCascadeTrajectory",
        {
            "cascade_id": cascade_id,
            "trajectory_verbosity": 2,  # PROD_UI — minimal data
        },
    )
    status_raw = resp.get("status", 0)
    if isinstance(status_raw, str):
      status = STATUS_STR_TO_INT.get(status_raw, 0)
    else:
      status = status_raw
    num_steps = resp.get("numTotalSteps", resp.get("num_total_steps", 0))
    return {
        "status": status,
        "status_name": STATUS_NAMES.get(status, f"UNKNOWN({status})"),
        "num_steps": num_steps,
        "error": None,
    }
  except Exception as e:
    return {
        "status": -1,
        "status_name": "ERROR",
        "num_steps": 0,
        "error": str(e),
    }


def get_agent_last_step(port, csrf_token, cascade_id, offset=0):
  """Get trajectory step(s) for an agent starting at offset.

  Args:
    port: Language Server port number.
    csrf_token: CSRF token for LS authentication.
    cascade_id: Agent cascade ID.
    offset: Step offset to start from (0-indexed).

  Returns:
    Raw response dict, or None on error.
  """
  try:
    return ls_request(
        port,
        csrf_token,
        "GetCascadeTrajectorySteps",
        {
            "cascade_id": cascade_id,
            "step_offset": offset,
            "trajectory_verbosity": 1,  # DEBUG — more detail
        },
    )
  except Exception:
    return None


# ---------------------------------------------------------------------------
# Trajectory Step Parsing
# ---------------------------------------------------------------------------


def extract_step_text(step):
  """Extract human-readable text from a trajectory step.

  Field names verified against real API output from
  /tmp/ztab_test_runs/2026-06-22_22-18_session/agent_a_thoughts.jsonl

  Args:
    step: A single trajectory step dict from the LS API.

  Returns:
    Human-readable string representation of the step content.
  """
  step_type = step.get("type", "")

  if step_type == "CORTEX_STEP_TYPE_USER_INPUT":
    ui = step.get("userInput", {})
    text = ui.get("userResponse", "")
    if not text:
      items = ui.get("items", [])
      if items and isinstance(items[0], dict):
        text = items[0].get("text", "")
    return text

  if step_type == "CORTEX_STEP_TYPE_PLANNER_RESPONSE":
    pr = step.get("plannerResponse", {})
    return pr.get("thinking", "")

  if step_type == "CORTEX_STEP_TYPE_MCP_TOOL":
    mt = step.get("mcpTool", {})
    tc = mt.get("toolCall", {})
    name = tc.get("name", "unknown")
    args = tc.get("argumentsJson", "")
    result = mt.get("resultString", "")
    text = f"Tool: {name}"
    if args:
      text += f"\nArgs: {args}"
    if result:
      text += f"\nResult: {result[:200]}"
    return text

  if step_type == "CORTEX_STEP_TYPE_RUN_COMMAND":
    rc = step.get("runCommand", {})
    cmd = rc.get("commandLine", "")
    exit_code = rc.get("exitCode", "?")
    return f"$ {cmd}  (exit={exit_code})"

  if step_type == "CORTEX_STEP_TYPE_VIEW_FILE":
    vf = step.get("viewFile", {})
    return f"File: {vf.get('absolutePathUri', '?')}"

  if step_type == "CORTEX_STEP_TYPE_ERROR_MESSAGE":
    em = step.get("errorMessage", {})
    err = em.get("error", {})
    if isinstance(err, dict):
      return err.get("shortError", err.get("userErrorMessage", ""))
    return str(err)

  if step_type == "CORTEX_STEP_TYPE_CODE_ACTION":
    ca = step.get("codeAction", {})
    return ca.get("description", "")

  if step_type == "CORTEX_STEP_TYPE_SYSTEM_MESSAGE":
    sm = step.get("systemMessage", {})
    return sm.get("message", "")

  if step_type == "CORTEX_STEP_TYPE_GREP_SEARCH":
    gs = step.get("grepSearch", {})
    return f"grep '{gs.get('query', '')}' in {gs.get('searchPathUri', '')}"

  if step_type == "CORTEX_STEP_TYPE_GENERIC":
    td = step.get("taskDetails", {})
    return td.get("description", "")

  if step_type in (
      "CORTEX_STEP_TYPE_CHECKPOINT",
      "CORTEX_STEP_TYPE_CONVERSATION_HISTORY",
  ):
    return ""

  # Fallback: show what keys exist so we know what we missed
  keys = [k for k in step if k not in ("type", "status", "metadata")]
  return f"[unhandled] keys={keys}"


def extract_field_from_step(step, field_name, tool_names, search_args,
                           search_response):
  """Extracts a named field from a ZTAB MCP step.

  Searches for a JSON key-value pair matching '"<field_name>": "<value>"'
  across the specified parts of the step (arguments, response, or both).

  Args:
    step: The trajectory step dict.
    field_name: The JSON field name to extract (e.g. 'session_id',
      'participant_token').
    tool_names: Tool name (str) or collection of names (list/tuple/set) to
      restrict extraction to. Pass None to allow any ZTAB tool.
    search_args: If True, searches the tool arguments (request).
    search_response: If True, searches the tool response (result).
  """
  if step.get("type") != "CORTEX_STEP_TYPE_MCP_TOOL":
    return None
  mt = step.get("mcpTool", {})
  tc = mt.get("toolCall", {})
  name = tc.get("name", "")
  if not name.startswith("ztab_"):
    return None

  # Tool name filter
  if tool_names:
    if isinstance(tool_names, str):
      if name != tool_names:
        return None
    elif name not in tool_names:
      return None

  text_sources = []

  # Search arguments (request)
  if search_args:
    args = tc.get("argumentsJson", tc.get("args", ""))
    if isinstance(args, str):
      text_sources.append(args)
    elif isinstance(args, dict):
      text_sources.append(json.dumps(args))

  # Search response (result)
  if search_response:
    text_sources.append(mt.get("resultString", ""))
    response = mt.get("response", {})
    if isinstance(response, str):
      text_sources.append(response)
    elif isinstance(response, dict):
      text_sources.append(json.dumps(response))

    step_response = step.get("response", step.get("content", ""))
    if isinstance(step_response, str):
      text_sources.append(step_response)

  if not text_sources:
    return None

  combined_text = "\n".join(text_sources)
  match = re.search(
      rf'"{field_name}"\s*:\s*"([a-zA-Z0-9]+)"', combined_text
  )
  if match:
    return match.group(1)
  return None


def extract_session_id(step, tool_names, search_args, search_response):
  """Extracts session_id from a ZTAB MCP step.

  Convenience wrapper around extract_field_from_step for session_id.

  Args:
    step: The trajectory step dict.
    tool_names: Tool name (str) or collection of names (list/tuple/set) to
      restrict extraction to. Pass None to allow any ZTAB tool.
    search_args: If True, searches the tool arguments (request).
    search_response: If True, searches the tool response (result).
  """
  return extract_field_from_step(
      step, "session_id", tool_names, search_args, search_response
  )


def format_and_print_step(step, label, step_idx):
  """Pretty-print a single trajectory step to stderr.

  Args:
    step: A single trajectory step dict from the LS API.
    label: Agent label string (e.g., "Agent 1").
    step_idx: Step index number.
  """
  ts = time.strftime("%H:%M:%S")
  step_type = step.get("type", "unknown")

  # Header line
  print(f"  [{label}] [{ts}] Step {step_idx}: {step_type}", file=sys.stderr)

  content = extract_step_text(step)
  if not content:
    return

  lines = content.split("\n")
  max_lines = 15
  for line in lines[:max_lines]:
    if len(line) > 120:
      line = line[:117] + "..."
    print(f"    | {line}", file=sys.stderr)
  if len(lines) > max_lines:
    print(f"    | ... [{len(lines) - max_lines} more lines]", file=sys.stderr)


# ---------------------------------------------------------------------------
# Timer Detection (F7/B1)
# ---------------------------------------------------------------------------


def detect_active_timer(port, csrf_token, cascade_id, last_scanned_step,
                        last_seen_step):
  """Check if the agent scheduled a timer via the schedule tool.

  B1/F7: Scans trajectory steps incrementally from last_scanned_step to
  last_seen_step. Returns the duration of the most recent timer found
  (which represents the agent's final scheduling intent), or 0 if none.

  Args:
    port: Language Server port.
    csrf_token: CSRF token for LS requests.
    cascade_id: Agent cascade ID.
    last_scanned_step: Step offset to start scanning from (inclusive).
    last_seen_step: Current last known step (exclusive upper bound).

  Returns:
    timer_duration_secs (int) or 0 if no active timer found.
  """
  try:
    resp = ls_request(
        port, csrf_token, "GetCascadeTrajectorySteps",
        {
            "cascade_id": cascade_id,
            "step_offset": last_scanned_step,
            "trajectory_verbosity": 1,
        },
    )
    if not resp:
      return 0
    # Scan in reverse to find the most recent timer (agent's final intent).
    for step in reversed(resp.get("steps", [])):
      tc = step.get("toolCall")
      if not tc:
        tc = step.get("metadata", {}).get("toolCall")

      if tc:
        name = tc.get("name", "")
        if name == "schedule":
          args_json = tc.get("argumentsJson", "")
          try:
            args = json.loads(args_json) if args_json else {}
          except json.JSONDecodeError:
            args = {}
          duration = args.get("DurationSeconds", "")
          if duration:
            return int(duration)
          # Cron jobs also count — use a default long grace.
          cron = args.get("CronExpression", "")
          if cron:
            return 120
  except Exception:
    pass
  return 0


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def stream_agent_thoughts(
    port, csrf_token, cascade_id, label, last_seen_step=0, log_file=None
):
  """Fetch and print any new trajectory steps since last_seen_step.

  Args:
    port: Language Server port number.
    csrf_token: CSRF token for LS authentication.
    cascade_id: Agent cascade ID.
    label: Agent label string (e.g., "Agent 1").
    last_seen_step: Step offset of the last seen step.
    log_file: Optional path to append step JSON to.

  Returns:
    Tuple of (new_last_seen_step, status_info_dict).
  """
  info = get_agent_status(port, csrf_token, cascade_id)
  num_steps = info["num_steps"]

  if num_steps > last_seen_step:
    steps_resp = get_agent_last_step(
        port, csrf_token, cascade_id, offset=last_seen_step
    )
    if steps_resp:
      for step in steps_resp.get("steps", []):
        step_idx = last_seen_step
        last_seen_step += 1
        format_and_print_step(step, label, step_idx)

        if log_file:
          with open(log_file, "a") as f:
            f.write(json.dumps(step) + "\n")

  return last_seen_step, info
