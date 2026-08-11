# Calendar Scheduling Scenario

A ZTAB scenario that uses the `ScheduleOverlap` policy to find overlapping
time slots between multiple participants' schedules.

## Files

| File | Purpose |
|:-----|:--------|
| `policy.json` | The `ScheduleOverlap` policy definition — prompt template, input/output schemas. Loaded by the TEE server via `--policy_dir`. |
| `test_data.py` | Participant schedules and expected overlaps for testing. |
| `scenario.py` | `CalendarScenario` class implementing the `Scenario` interface. |

## Usage

### As a deployment artifact

Point the TEE server at this directory:

```bash
./ztab_server --policy_dir=examples/calendar/ --model_path=... \
    --creator_token=SECRET   # optional admission control
```

### As test data

```bash
# Test prompt quality
python3 -m test.test_prompt \
    --scenario examples.calendar.scenario:CalendarScenario \
    --host localhost --port 8000

# Test session lifecycle
python3 -m test.test_session \
    --scenario examples.calendar.scenario:CalendarScenario \
    --host localhost --port 8000 --verifier noop

# With admission control testing:
python3 -m test.test_session \
    --scenario examples.calendar.scenario:CalendarScenario \
    --host localhost --port 8000 --verifier noop \
    --test_admission --creator_token SECRET
```

## Building Your Own Scenario

To create a new scenario (e.g., `examples/budget/`):

1. Create `examples/budget/policy.json` with your policy definition.
2. Create `examples/budget/test_data.py` with test inputs and expected results.
3. Create `examples/budget/scenario.py` implementing the `Scenario` ABC from
   `test/scenario_base.py`.
4. Run with `--scenario examples.budget.scenario:BudgetScenario`.
