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

#ifndef ZTAB_SERVER_POLICY_REGISTRY_H_
#define ZTAB_SERVER_POLICY_REGISTRY_H_

#include <string>

#include "absl/container/flat_hash_map.h"
#include "absl/status/statusor.h"

namespace ztab {

// Defines the processing behavior for a session policy class.
//
// The prompt_template uses placeholders:
//   {num_participants} — replaced with the count of participants
//   {inputs}           — replaced with delimiter-wrapped participant inputs
struct PolicyDefinition {
  std::string prompt_template;
  std::string default_input_schema_json;
  std::string default_output_schema_json;
};

// Registry of built-in policy classes. Phase 1 supports a single hardcoded
// policy ("ExtractAndResolve"). Additional policy classes will be registered
// here in Phase 2+.
//
// The PolicyRegistry is NOT where prompt templates are stored for security
// reasons (agents cannot supply arbitrary templates). Templates are part of
// the trusted codebase — changing them requires a new image build, which
// changes the attestation digest.
class PolicyRegistry {
 public:
  PolicyRegistry();

  // Returns the definition for a known policy class, or NOT_FOUND.
  absl::StatusOr<const PolicyDefinition*> GetPolicy(
      const std::string& policy_class) const;

 private:
  absl::flat_hash_map<std::string, PolicyDefinition> policies_;
};

}  // namespace ztab

#endif  // ZTAB_SERVER_POLICY_REGISTRY_H_
