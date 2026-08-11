# ZTAB Test Infrastructure

This directory contains the testing infrastructure for ZTAB.
It is structured into different levels to verify
components in isolation before running full end-to-end tests.

## Test Levels

We categorize tests into two main levels:

### Level 1: Component-Level Tests (This Directory)

These tests verify specific components of the ZTAB system in isolation using
direct Python scripts. They do not run the full agent architecture or MCP
server, and they do not require an active Language Server.

*   **Prompt Quality Test (`test_prompt.py`)**:
    Verifies that the LLM can correctly parse prompt templates and produce
    expected structured outputs. It formats the prompt with test inputs and
    sends it directly to the TEE server's Echo RPC.
*   **Session Lifecycle Test (`test_session.py`)**:
    Verifies the TEE server's state machine, input
    aggregation, and policy enforcement. It drives the
    session lifecycle (create, join, submit, get result)
    using direct gRPC calls. Supports `--test_admission`
    mode for 11 additional admission control tests (token
    gating, rejection, idempotency).
*   **Agent Unit Test (`test_agent_unit.py`)**:
    Verifies agent-side components: verifier factory,
    backends.json parsing, MCP tool registration, channel
    caching, and creator_token injection. 9 unit tests.

### Level 2: End-to-End Harness Tests (`test/harness/`)

*   **Cold-Start Test Harness (`test_cold_start.sh`)**:
    This is the production-fidelity end-to-end test. It
    launches the actual Language Server, provisions
    isolated agent sandboxes, installs the ZTAB MCP server
    dynamically, and monitors the real agents as they
    coordinate and execute the session lifecycle using MCP
    tools.

    For detailed E2E testing instructions, see
    [test/harness/README.md](harness/README.md).

---

## Component-Level Usage (Level 1)

These component tests are fast to execute and useful for
quick validation of prompt templates or TEE server changes.

### 1. Run Prompt Quality Test

Requires a running TEE server with a model loaded (see
[tee/README.md](../tee/README.md)):

```bash
python3 -m test.test_prompt \
    --scenario examples.calendar.scenario:CalendarScenario \
    --host localhost --port 8000 \
    --show_prompt --show_response
```

### 2. Run Session Lifecycle Test

Requires a running TEE server:

```bash
python3 -m test.test_session \
    --scenario examples.calendar.scenario:CalendarScenario \
    --host localhost --port 8000 --verifier noop
```

### 3. Run Session Lifecycle Test with Admission Control

```bash
python3 -m test.test_session \
    --scenario examples.calendar.scenario:CalendarScenario \
    --host localhost --port 8000 --verifier noop \
    --test_admission --creator_token SECRET
```

---

## Technical Details

*   **No Build System**: All component scripts run with standard `python3`.
*   **Proto Stubs**: Protobuf python stubs are pre-compiled and committed in
    `agent/pb2/` (re-generate if needed using `agent/regen_protos.sh`).
*   **Venv**: For manual component testing, you can use the shared venv (see
    setup in [test/harness/README.md](harness/README.md)).
