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

"""Calendar scheduling scenario for ZTAB.

Implements the Scenario interface for the ScheduleOverlap policy. Provides
test cases with participant schedules and validates that the LLM correctly
identifies overlapping time slots.

This scenario is both documentation (showing how to build a ZTAB scenario)
and a deployment artifact (policy.json is loaded by --policy_dir).
"""

import json
import os
import sys

# Add the ztab root to sys.path so we can import test.scenario_base.
_ZTAB_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if _ZTAB_ROOT not in sys.path:
    sys.path.insert(0, _ZTAB_ROOT)

from test.scenario_base import Scenario, TestCase
from examples.calendar import test_data


class CalendarScenario(Scenario):
    """Calendar scheduling overlap scenario."""

    @property
    def name(self):
        return "calendar"

    @property
    def policy_json_path(self):
        return os.path.join(os.path.dirname(__file__), "policy.json")

    @property
    def policy_class(self):
        return "ScheduleOverlap"

    @property
    def num_participants(self):
        return 2

    def get_test_cases(self):
        return [
            TestCase(
                name="full_overlap",
                inputs=[
                    {"available_slots": test_data.PARTICIPANT_A_SLOTS},
                    {"available_slots": test_data.PARTICIPANT_B_SLOTS},
                ],
                expected_result=test_data.EXPECTED_OVERLAP,
            ),
            # Future: add partial_overlap, no_overlap, many_participants, etc.
        ]

    def validate_result(self, result_json, expected, allow_subset=False):
        """Validate that the LLM output matches expected overlapping slots.

        Args:
            result_json: Raw JSON string or parsed list from LLM output.
            expected: Expected list of ISO 8601 datetime strings.
            allow_subset: If True, accept partial matches (at least 1 correct).

        Returns:
            Tuple of (passed: bool, detail: str).
        """
        try:
            result = (json.loads(result_json)
                      if isinstance(result_json, str) else result_json)
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON: {e}"

        expected_set = set(expected)
        got = set(result)

        if got == expected_set:
            return True, "Exact match."
        if allow_subset and got.issubset(expected_set) and len(got) >= 1:
            missing = expected_set - got
            return True, f"Subset match (missing: {missing})."
        return False, f"Expected {sorted(expected_set)}, got {sorted(got)}."
