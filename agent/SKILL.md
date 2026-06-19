---
name: ztab
description: >-
  Coordinate privately with other agents through a TEE broker.
  Use when you need to find a shared meeting time, negotiate,
  or reach any consensus involving private data from multiple
  parties — without revealing your raw data to anyone.
---

# ZTAB — Zero-Trust Agent Broker

## What ZTAB Does

ZTAB lets multiple agents submit private data (like calendar
availability) to an LLM running inside a Trusted Execution
Environment (TEE). The LLM computes a shared outcome (like
overlapping meeting times) and returns it to all participants.
No participant ever sees another's raw data.

All connections are cryptographically verified against the TEE's
hardware attestation report before any data is sent.

## Available Tools

| Tool | Purpose |
|:-----|:--------|
| `ztab_create_session` | Create a new session (you are the first participant) |
| `ztab_join_session` | Join an existing session by session_id |
| `ztab_accept_policy` | Accept the session's processing policy |
| `ztab_submit_input` | Submit your private data |
| `ztab_get_result` | Retrieve the shared result |
| `ztab_get_session_status` | Check session progress |
| `ztab_test_connection` | Test server connectivity |
| `ztab_list_backends` | List configured TEE backends |

## Named Backends

The MCP tools use a Named Backends architecture. Connection
parameters (host, port, verifier, digest) are NOT passed as
tool arguments — they are configured in `~/.ztab/backends.json`
and referenced by `backend` name.

Each tool accepts an optional `backend` parameter. If omitted,
the default backend is used. Call `ztab_list_backends` to see
what backends are currently configured.

## Workflow: Scheduling a Meeting

### If you are creating the session:

1. Call `ztab_create_session` with `policy_class="ExtractAndResolve"`
   and `expected_participants=2` (or more). Optionally pass `backend`
   to select a specific TEE backend (otherwise the default is used).
2. Save the returned `session_id` and `participant_token`.
3. Share the `session_id` with the other participant(s) — e.g., via
   message, email, or shared context. Also share which TEE backend
   to use (by name or host:port).
4. Wait for all participants to join and accept (you can check
   progress via `ztab_get_session_status`).
5. Once the session is SEALED, call `ztab_submit_input` with your
   private data as a JSON string:
   ```json
   {"available_slots": ["2026-07-15T10:00:00Z", "2026-07-15T14:00:00Z"]}
   ```
6. Poll `ztab_get_result` until the session state is CLOSED.
7. The result is a JSON array of overlapping time slots.

### If you are joining an existing session:

1. You will receive a `session_id` and information about which TEE
   to connect to. If the TEE is not already in your backends config,
   add it first (see "Connecting to a New TEE" below).
2. Call `ztab_join_session` with the `session_id` and optionally
   `backend` if using a non-default backend.
3. Review the returned policy. If acceptable, call
   `ztab_accept_policy` with your `participant_token`.
4. Call `ztab_submit_input` with your private data.
5. Poll `ztab_get_result` until the session state is CLOSED.

## Input Format

For the `ExtractAndResolve` policy, your input must be a JSON
object with an `available_slots` array of ISO 8601 datetime
strings:

```json
{
  "available_slots": [
    "2026-07-15T10:00:00Z",
    "2026-07-15T14:00:00Z",
    "2026-07-16T09:00:00Z"
  ]
}
```

## Bootstrapping (First-Time Setup)

1. Run the install script from the ZTAB repository:

   ```bash
   bash agent/install_mcp.sh
   ```

   This creates a Python virtualenv with ZTAB dependencies and
   generates a default `~/.ztab/backends.json` file with a
   `dev-local` backend pointing to `localhost:8000`.

2. Copy the output JSON into your MCP configuration file
   (`~/.gemini/config/mcp_config.json`) under the `mcpServers` key.

3. If you need to connect to a different TEE server, see
   "Connecting to a New TEE" below.

## Connecting to a New TEE

To connect to a TEE server not already in your configuration:

1. Open `~/.ztab/backends.json`.
2. Add a new entry to the `backends` array:

   ```json
   {
     "backend_id": "my-tee",
     "name": "Description of this TEE",
     "host": "HOST_ADDRESS",
     "port": PORT_NUMBER,
     "verifier": "noop",
     "expected_digest": "",
     "allow_debug_tee": true
   }
   ```

   For production GCP Confidential Space TEEs, use:
   - `"verifier": "ita"`
   - `"expected_digest": "sha256:ACTUAL_DIGEST"`
   - `"allow_debug_tee": false`

3. Optionally set `"default_backend": "my-tee"` at the top level
   if this should be the default.

4. The MCP server reads the config at startup. If it is already
   running, it must be restarted for changes to take effect.

5. Pass `backend: "my-tee"` when calling MCP tools, or omit
   `backend` to use the default.

You can call `ztab_list_backends` to see what backends are
currently configured.
