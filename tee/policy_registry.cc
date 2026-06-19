// Copyright 2026 Google LLC.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

#include "policy_registry.h"

#include "absl/status/status.h"
#include "absl/strings/str_cat.h"

namespace ztab {

PolicyRegistry::PolicyRegistry() {
  // Phase 1: Single built-in policy class for calendar scheduling.
  //
  // ExtractAndResolve: Aggregates private inputs from multiple participants,
  // uses the LLM to extract and compute a shared result (e.g., overlapping
  // time slots), and returns only the computed result — not the raw inputs.
  policies_["ExtractAndResolve"] = PolicyDefinition{
      .prompt_template = R"(You are a scheduling assistant running inside a Trusted Execution Environment.
Below are availability inputs from {num_participants} participants.
Each participant's data is private — you MUST NOT reveal any individual's schedule in your output.

{inputs}

Find all time slots where ALL participants are available.
Output ONLY a JSON array of ISO 8601 datetime strings.
Example: ["2026-07-01T10:00:00Z", "2026-07-01T14:00:00Z"]
Do not include any explanation, reasoning, or commentary. Output only the JSON array.)",

      .default_input_schema_json = R"({
  "type": "object",
  "properties": {
    "available_slots": {
      "type": "array",
      "items": {
        "type": "string",
        "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(:\\d{2})?Z$"
      },
      "minItems": 1,
      "maxItems": 200
    }
  },
  "required": ["available_slots"],
  "additionalProperties": false
})",

      .default_output_schema_json = R"({
  "type": "array",
  "items": {
    "type": "string",
    "pattern": "^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}(:\\d{2})?Z$"
  },
  "minItems": 0,
  "maxItems": 200
})",
  };
}

absl::StatusOr<const PolicyDefinition*> PolicyRegistry::GetPolicy(
    const std::string& policy_class) const {
  auto it = policies_.find(policy_class);
  if (it == policies_.end()) {
    return absl::NotFoundError(
        absl::StrCat("Unknown policy class: '", policy_class,
                     "'. Available: ExtractAndResolve"));
  }
  return &it->second;
}

}  // namespace ztab
