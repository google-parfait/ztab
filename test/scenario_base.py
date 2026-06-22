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

"""Abstract base class for ZTAB test scenarios.

A Scenario defines a complete test case collection for a specific policy:
- The policy class name and JSON file path.
- A set of TestCases, each with inputs, expected results, and validation mode.
- A validate_result() method for checking LLM output quality.
- A format_prompt() utility for direct Echo RPC testing.

Concrete scenario implementations live in examples/<scenario>/scenario.py.
"""

import abc
import json
from dataclasses import dataclass


@dataclass
class TestCase:
    """A single test case within a scenario."""
    name: str
    inputs: list[dict]
    expected_result: list
    allow_subset: bool = False


class Scenario(abc.ABC):
    """Base class for ZTAB test scenarios.

    Subclasses must implement all abstract methods. Each scenario corresponds
    to a policy JSON file in examples/<scenario>/ and provides test data and
    validation logic for that policy.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable scenario name (e.g., 'calendar')."""
        ...

    @property
    @abc.abstractmethod
    def policy_json_path(self) -> str:
        """Absolute path to the policy JSON file."""
        ...

    @property
    @abc.abstractmethod
    def policy_class(self) -> str:
        """The policy_class string as registered in the TEE server."""
        ...

    @property
    @abc.abstractmethod
    def num_participants(self) -> int:
        """Number of participants required by this scenario."""
        ...

    @abc.abstractmethod
    def get_test_cases(self) -> list[TestCase]:
        """Return all test cases for this scenario."""
        ...

    @abc.abstractmethod
    def validate_result(self, result_json, expected, allow_subset=False
                        ) -> tuple[bool, str]:
        """Validate LLM output against expected result.

        Args:
            result_json: Raw JSON string or parsed object from LLM output.
            expected: The expected result (from TestCase.expected_result).
            allow_subset: If True, accept partial matches.

        Returns:
            Tuple of (passed: bool, detail: str).
        """
        ...

    def load_policy(self) -> dict:
        """Load and return the policy JSON."""
        with open(self.policy_json_path) as f:
            return json.load(f)

    def format_prompt(self, inputs):
        """Format the prompt template with participant inputs.

        NOTE: This produces a simplified prompt without the randomized
        delimiter suffixes used by the TEE server's session_manager.cc.
        This is intentional — the server uses randomized delimiters like
        <<<PARTICIPANT_1_INPUT_BEGIN_a3f7c2e1>>> to prevent prompt injection.
        This method uses static delimiters because it is only consumed by
        test_prompt.py for direct Echo RPC testing, where the delimiter
        security mechanism is not in the code path.

        Args:
            inputs: List of input dicts, one per participant.

        Returns:
            Formatted prompt string ready for Echo RPC.
        """
        policy = self.load_policy()
        template = policy["prompt_template"]
        blocks = []
        for i, inp in enumerate(inputs):
            blocks.append(
                f"<<<PARTICIPANT_{i+1}_INPUT_BEGIN>>>\n"
                f"{json.dumps(inp)}\n"
                f"<<<PARTICIPANT_{i+1}_INPUT_END>>>"
            )
        return template.format(
            num_participants=len(inputs),
            inputs="\n\n".join(blocks),
        )
