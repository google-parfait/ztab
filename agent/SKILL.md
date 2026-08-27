---
name: ztab
description: >-
  Coordinate privately with other agents through a TEE broker.
  Use when you need to reach a consensus involving private data
  from multiple parties — without revealing your raw data to anyone.
---

# ZTAB

ZTAB lets multiple agents submit private data to an LLM running
inside a Trusted Execution Environment (TEE). The LLM computes a
shared outcome and returns it to all participants. No participant
ever sees another's raw data.

---

## Part 1: Installation & Configuration

Follow this section to install the ZTAB MCP server and configure
your TEE backend. You do NOT need to read Part 2 until the ZTAB
tools are available in your environment.

### First-Time Setup

Run the install script with all your backend details:

```bash
bash agent/install_mcp.sh --add_backend NAME HOST PORT \
    --set_default [options]
```

This single command does everything:
- Creates a Python virtualenv with ZTAB dependencies
- Configures the specified backend in `~/.ztab/backends.json`
- Registers the `ztab` MCP server in your agent config
  (`~/.gemini/config/mcp_config.json`)

You do NOT need to edit any JSON files manually.

**Checkpoint:** Verify the script prints
`✅ Registered ztab in ~/.gemini/config/mcp_config.json`.

If no `--add_backend` is given, a default `dev-local` backend
pointing to `localhost:8000` is created.

The script is safe to re-run. It skips venv creation if the
environment already exists and is functional.

### Adding More Backends

To add another backend after initial setup, run the same
script again with new `--add_backend` details:

```bash
bash agent/install_mcp.sh --add_backend prod-tee HOST PORT \
    --verifier ita \
    --digest "sha256:ACTUAL_DIGEST" \
    --no_debug_tee \
    --set_default
```

The venv and registration steps will be skipped if already
done. Only the new backend entry is added.

#### Examples

```bash
# Development TEE (no attestation):
bash agent/install_mcp.sh --add_backend my-tee HOST PORT \
    --set_default

# With admission control (creator token):
bash agent/install_mcp.sh --add_backend my-tee HOST PORT \
    --creator_token TOKEN --set_default

# Production GCP Confidential Space TEE:
bash agent/install_mcp.sh --add_backend prod-tee HOST PORT \
    --verifier ita \
    --digest "sha256:ACTUAL_DIGEST" \
    --no_debug_tee \
    --set_default
```

#### Options

- `--verifier TYPE` — `noop` (default, no attestation) or `ita`
- `--digest DIGEST` — Expected container image digest
- `--allow_debug_tee` — Accept debug TEE (default)
- `--no_debug_tee` — Reject debug TEE (for production)
- `--creator_token TOKEN` — Pre-shared token for admission control
- `--set_default` — Make this the default backend
- `--do_not_register` — Skip `mcp_config.json` registration
  (use when the default registration path doesn't apply)

### Manual JSON Edit

Only use this if the CLI is not available or if your
environment requires a non-standard configuration path.

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

   If the TEE requires admission control, add:
   - `"creator_token": "TOKEN_VALUE"`

   For production GCP Confidential Space TEEs, use:
   - `"verifier": "ita"`
   - `"expected_digest": "sha256:ACTUAL_DIGEST"`
   - `"allow_debug_tee": false`

3. Optionally set `"default_backend": "my-tee"` at the top level
   if this should be the default.

### After Setup

The MCP server reads the config at startup. If it is already
running, it must be restarted for changes to take effect.

Pass `backend: "my-tee"` when calling MCP tools, or omit
`backend` to use the default.

You can call `ztab_list_backends` to see what backends are
currently configured.

### Named Backends

Connection parameters (host, port, verifier, digest) are
configured in `~/.ztab/backends.json` and referenced by name.

Each tool accepts an optional `backend` parameter. If omitted,
the default backend is used. Call `ztab_list_backends` to see
configured backends.

---

## Part 2: Using ZTAB

### What is a ZTAB Session?

A **session** is a single multi-agent computation. For example, if two
agents want to find a common meeting time without revealing their
calendars to each other, they create a ZTAB session for that purpose.

- Each session runs on a ZTAB server inside a TEE.
- Each session has a fixed number of participants, set at creation time.
- If agents need to compute two different things (e.g., schedule two
  different meetings), those are two separate sessions.

### Roles

Every participant in a session has one of two roles:

- **CREATOR**: The agent that creates the session. The CREATOR calls
  `ztab_create_session`, gets back an `invitation_token`, and shares it
  with the other participants (typically via the user).

- **JOINER**: Every other agent. A JOINER receives the `invitation_token`
  from the user or another channel, then calls `ztab_join_session`.

Both roles must submit their private input. Both roles receive the
shared result.

### Session State Machine

Every session progresses through these states:

```
OPEN → SEALED → CALCULATING → CLOSED
  ↓       ↓         ↓
  ABORTED ABORTED   ABORTED
```

| State | Meaning | What triggers the next transition |
|:------|:--------|:----------------------------------|
| `OPEN` | Session created by the creator calling `ztab_create_session`. Waiting for all participants to join and accept the policy. | All joiners must now call `ztab_join_session` then `ztab_accept_policy`. |
| `SEALED` | All joiners have accepted. Waiting for all inputs. | All participants (creator and joiners) must now call `ztab_submit_input`. |
| `CALCULATING` | TEE is running LLM inference on the submitted inputs. No more input can be accepted. | Automatic. Wait for completion. |
| `CLOSED` | Done. Result is available. | Terminal state. Call `ztab_get_result` to fetch the result. |
| `ABORTED` | Session failed (timeout, error, or cancellation). | Terminal state. |

### Tools

| Tool | Purpose | Required params |
|:-----|:--------|:----------------|
| `ztab_test_connection` | Verify TEE server is reachable | `message` |
| `ztab_list_backends` | List configured backends | (none) |
| `ztab_create_session` | Create a new session | `policy_class`, `expected_participants` |
| `ztab_join_session` | Join an existing session | `invitation_token` |
| `ztab_accept_policy` | Accept the session policy | `participant_token` |
| `ztab_submit_input` | Submit your private data | `participant_token`, `input_json` |
| `ztab_get_session_status` | Check session state and participant counts | `participant_token` |
| `ztab_get_result` | Get the final result (when CLOSED), or session state (if not yet) | `participant_token` |

All tools accept an optional `backend` parameter. Omit it to use the default.

> **RULE: Call tools by their native names (e.g., `ztab_test_connection`).
> Do NOT use shell `mcp` commands, `cli.py`, or `run_command` to invoke
> ZTAB operations. These bypass attestation and will fail.**

### How to Poll and Yield

When waiting for a state change (participants joining, inputs arriving,
or calculation completing):

1. Call `schedule(DurationSeconds="30", Prompt="Poll ZTAB session")`.
2. **STOP. Do not call any other tools this turn.** Yield control immediately.
3. When the timer wakes you, resume from the polling step.

> **RULE: Never busy-loop.** Every poll must be followed by a yield.
> Never call a status/result tool and then immediately call it again
> in the same turn without yielding in between.

> **RULE: One timer at a time.** Do not schedule a new timer if a
> previous one is still pending. Cancel the old timer first with
> `manage_task(Action="kill", TaskId="<id>")`.

---

### CREATOR Procedure

You are the CREATOR if you are initiating a new session. Follow
these steps in order.

#### CREATOR Step 1: Create the session

> **GUARD: Do NOT create a session if you have already created one
> for the same task.** Search your conversation history for a prior
> `ztab_create_session` call. If you find one, and it's for the same task,
> extract the
> `invitation_token` and `participant_token` from that response, and go to
> CREATOR Step 2 (share the invitation token).

Otherwise, call `ztab_create_session`:
- `policy_class`: The policy to use (e.g., `"ScheduleOverlap"`)
- `expected_participants`: Total count including yourself (≥ 2)

Save from the response:
- `invitation_token` — share this with other participants
- `participant_token` — keep this secret, use in all subsequent calls

After that, proceed to CREATOR Step 2 (share the invitation token).

#### CREATOR Step 2: Share the invitation token

NOTE: You only need to do this once. If you already executed this step,
go to CREATOR Step 3 (wait for others).

Otherwise, tell the user the `invitation_token` so they can share it with
the other participants. Example response:

> "I created a ZTAB session. Please share this invitation token with
> the other participant so their agent can join: `<invitation_token>`"

After that, proceed to CREATOR Step 3 (wait for others).

#### CREATOR Step 3: Wait for others

This step is a loop. You may need to repeat it many times. Keep
going until the state is no longer `OPEN` as described below.
You are waiting for everyone to learn about the session, join, and
accept the policy. This can take time.

Call `ztab_get_session_status` with your `participant_token`.

Read the `state` field in the response:

- **If `OPEN`:** Not all participants have joined and accepted yet.
  → Poll, then set a 30-second timer and yield (see "How to
  Poll and Yield" above). Be patient — this may take several
  minutes.

- **If `SEALED`:** All participants joined and accepted.
  → Go to CREATOR Step 4 (submit your input).

- **If `CALCULATING`:** You are in this step by mistake. All inputs
  (including yours) have already been submitted. 
  → Go to CREATOR Step 5 (wait for the result).

- **If `CLOSED`:** → Go to CREATOR Step 5 (wait for the result).

- **If `ABORTED`:** → Go to CREATOR Step 6 (handle failure).

#### CREATOR Step 4: Submit your input

> **GUARD: This step is MANDATORY. The session will NOT produce
> results until ALL participants — including you — submit input.
> If you skip this step, the session will time out and ABORT.
> Keep in mind you must submit input EXACTLY ONCE per session.
> Do not resubmit your input if you already submitted.**

Call `ztab_submit_input`:
- `participant_token`: your participant token
- `input_json`: a JSON string containing your private data

Verify the response confirms success.
On error, go to CREATOR Step 6 (handle failure).
Otherwise, proceed to CREATOR Step 5 (wait for the result).

#### CREATOR Step 5: Wait for the result

This step is a loop. You may need to repeat it many times. Keep
going until you obtain the result, or the state is no longer
`SEALED` or `CALCULATING` as described below. You are waiting for
everyone to submit their input and for the system to calculate
the result. This can take time.

Call `ztab_get_result` with your `participant_token`.

Read the `state` field in the response:

- **If `SEALED`, or `CALCULATING`:**  Not done yet. Either not all
  participants have submitted their input (`SEALED`), or the system
  has not yet computed the result (`CALCULATING`).
  → **DO NOT repeatedly call `ztab_get_result` in a tight loop within
  the same turn.** Call `schedule(DurationSeconds="30", Prompt="Poll ZTAB session")`
  and STOP calling tools this turn (see "How to Poll and Yield" above).
  Be patient — TEE LLM inference may take several minutes.

- **If `OPEN`:** You are in this step by mistake. The session has not
  yet progressed to the `SEALED` state. Not everyone joined and accepted,
  and you cannot have submitted your input yet.
  → Go back to CREATOR Step 3 (wait for others).

- **If `CLOSED`:** The result is in the response.
  → Present the result to the user. **Done.**

- **If `ABORTED`:** → Go to CREATOR Step 6 (handle failure).

#### CREATOR Step 6: Handle failure

The session has aborted or a tool call failed. Report the error
to the user. **Done.**

---

### JOINER Procedure

You are the JOINER if someone gave you an `invitation_token` to join.
Follow these steps in order.

#### JOINER Step 1: Join the session

You must have an `invitation_token`. If you do not, ask the user for it.

Call `ztab_join_session` with the `invitation_token`.

Save from the response:
- `participant_token` — keep this secret, use in all subsequent calls
- The session policy — review it before proceeding

After that, proceed to JOINER Step 2 (accept the policy).

#### JOINER Step 2: Accept the policy

Review the policy returned in Step 1. If it is acceptable:

Call `ztab_accept_policy` with your `participant_token`.

After that, proceed to JOINER Step 3 (wait for others).

#### JOINER Step 3: Wait for others

This step is a loop. You may need to repeat it many times. Keep
going until the state is no longer `OPEN` as described below.
You are waiting for all participants to join and accept the
policy. This can take time.

Call `ztab_get_session_status` with your `participant_token`.

Read the `state` field in the response:

- **If `OPEN`:** Not all participants have joined and accepted yet.
  → Poll, then set a 30-second timer and yield (see "How to
  Poll and Yield" above). Be patient — this may take several
  minutes.

- **If `SEALED`:** All participants joined and accepted.
  → Go to JOINER Step 4 (submit your input).

- **If `CALCULATING`:** You are in this step by mistake. All inputs
  (including yours) have already been submitted.
  → Go to JOINER Step 5 (wait for the result).

- **If `CLOSED`:** → Go to JOINER Step 5 (wait for the result).

- **If `ABORTED`:** → Go to JOINER Step 6 (handle failure).

> NOTE: In a 2-participant session where you are the only Joiner,
> your `accept_policy` call in Step 2 typically triggers SEALED
> immediately (creators implicitly join and accept), so this
> step will normally pass through on the first check. In a larger
> session, you will have to wait for other joiners to take action.

#### JOINER Step 4: Submit your input

> **GUARD: This step is MANDATORY. The session will NOT produce
> results until ALL participants — including you — submit input.
> If you skip this step, the session will time out and ABORT.
> Keep in mind you must submit input EXACTLY ONCE per session.
> Do not resubmit your input if you already submitted.**

Call `ztab_submit_input`:
- `participant_token`: your participant token
- `input_json`: a JSON string containing your private data

Verify the response confirms success.
On error, go to JOINER Step 6 (handle failure).
Otherwise, proceed to JOINER Step 5 (wait for the result).

#### JOINER Step 5: Wait for the result

This step is a loop. You may need to repeat it many times. Keep
going until you obtain the result, or the state is no longer
`SEALED` or `CALCULATING` as described below. You are waiting for
everyone to submit their input and for the system to calculate
the result. This can take time.

Call `ztab_get_result` with your `participant_token`.

Read the `state` field in the response:

- **If `SEALED`, or `CALCULATING`:** Not done yet. Either not all
  participants have submitted their input (`SEALED`), or the system
  has not yet computed the result (`CALCULATING`).
  → Poll, then set a 30-second timer and yield (see "How to
  Poll and Yield" above). Be patient — this may take several
  minutes.

- **If `OPEN`:** You are in this step by mistake. The session has not
  yet progressed to the `SEALED` state. Not everyone joined and accepted,
  and you cannot have submitted your input yet.
  → Go back to JOINER Step 3 (wait for others).

- **If `CLOSED`:** The result is in the response.
  → Present the result to the user. **Done.**

- **If `ABORTED`:** → Go to JOINER Step 6 (handle failure).

#### JOINER Step 6: Handle failure

The session has aborted or a tool call failed. Report the error
to the user. **Done.**

---

### Where Am I? (Orientation on Wakeup)

When you wake up from a timer, receive a nudge, or are otherwise
resumed mid-session, walk through this decision tree to figure out
where you are. Answer each question by checking your conversation
history.

> **RULE: Never create a duplicate session.** Before calling
> `ztab_create_session`, check your conversation history. If you
> already have an `invitation_token` for the same task (e.g., scheduling
> the same meeting), resume that session — do not create a new
> one because other participants may already be working with you
> in the existing session, and you want to avoid the split brain
> scenario that will cause both sessions to fail. Creating sessions
> for *different* tasks is fine - distinct sessions are completely
> independent, and each one requires following its own protocol.

**Q1: What is your role?**

Search your conversation history and proceed as follows:

1. If you have invoked `ztab_create_session` or were told to create a session,
   you are the CREATOR. Proceed to Q2-CREATOR (below).

2. If you have invoked `ztab_join_session` or were told to join a session, you
   are a JOINER. Proceed to Q2-JOINER (below).

3. If neither of the above is the case, ask the user whether you are the Creator
   coordinating a task across a group of agents, or a joiner asked to
   participate in a task created by another agent.

**Q2-CREATOR: Have you created an `invitation_token` for this task?**

Search the conversation history for the `ztab_create_session` call associated
with this task. If you find it, the `invitation_token` will be in the result.
Go to Q3-CREATOR. Do not create a duplicate session for the same task.

Otherwise, if you cannot find `ztab_create_session` in your history, it means
you have not started yet. → go to **CREATOR Step 1** (create the session).

**Q2-JOINER: Have you received an `invitation_token` for this task?**

If you are a Joiner, you should have been given an `invitation_token`. Find it in the
conversation history. You may have already called `ztab_join_session`. If so,
you can extract `participant_token` from that call. If you find it, go to Q3-JOINER.

Otherwise, if you cannot find it, ask the user for it. Once you receive it, go
to Q3-JOINER.

**Q3-CREATOR: Have you called `ztab_submit_input` for this session?**

- **Yes** → go to **CREATOR Step 5** (wait for the result). Do not resubmit.
  You cannot submit input more than once per session.
- **No** → Continue to Q4-CREATOR.

**Q4-CREATOR: Have you shared the `invitation_token` with the user?**

- **No** → go to **CREATOR Step 2** (share invitation token).
- **Yes** → go to **CREATOR Step 3** (wait for others).
  The status response will tell you whether to wait (OPEN),
  submit (SEALED), handle failure (ABORTED), etc.

**Q3-JOINER: Have you called `ztab_submit_input` for this session?**

- **Yes** → go to **JOINER Step 5** (wait for the result).  Do not resubmit.
  You cannot submit input more than once per session.
- **No** → Continue to Q4-JOINER.

**Q4-JOINER: Have you called `ztab_accept_policy` for this session?**

- **Yes** → go to **JOINER Step 3** (wait for others).
- **No** → Continue to Q5-JOINER.

**Q5-JOINER: Have you called `ztab_join_session` for this session?**

- **Yes** → go to **JOINER Step 2** (accept policy).
- **No** → go to **JOINER Step 1** (join session).

---

### Things That Will Break Your Run

1. **Using shell `mcp` commands** — may connect to the wrong daemon.
   Always call tools natively (e.g., `ztab_test_connection`).
2. **Using `cli.py`** — does not know about ZTAB backends you configured,
   and may bypass attestation. Always use the MCP tools.
3. **Killing processes** — may break the MCP server. Never run
   `pkill`, `kill -9`, or similar.
4. **Modifying ZTAB source code** — the ZTAB codebase is read-only. If you
   think you may need to modify code, re-read these instructions carefully,
   pay attention to detail, and check your conversation history for context.
   You may have misunderstood the protocol or forgotten where you are in it.
5. **Setting `expected_digest: ""`** — empty string ≠ omitting the
   field. Omit it entirely or use `--allow_debug_tee`.
6. **Skipping `submit_input`** — the session will time out and ABORT.
   Both CREATOR and JOINER must submit.
7. **Creating a new session** when a session is underway for the same task.
   Will create a split brain that causes both sessions to fail.
8. **`PERMISSION_DENIED` on `ztab_create_session`** — the TEE server
   requires a `creator_token` but your `backends.json` does not have
   one configured for this backend. Fix: re-run
   `install_mcp.sh --creator_token TOKEN` or manually add
   `"creator_token": "TOKEN"` to the backend entry in
   `~/.ztab/backends.json`.
