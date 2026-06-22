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
#include "absl/status/status.h"
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

// Registry of policy classes loaded from JSON files at startup.
//
// SECURITY NOTE ON POLICY REGISTRATION:
// Policies are currently loaded from disk at deployment time via
// LoadFromDirectory(). This is intentional — policy text is part of the
// trusted codebase. For baked-in container images (GCP Confidential Space),
// the policy files are included in the image, so changing them changes the
// attestation digest, which clients can verify.
//
// FUTURE EXTENSIBILITY:
// A future extension could allow agents to define ad-hoc policies at session
// creation time (passing the prompt template in the CreateSession RPC). This
// would weaken the security model because arbitrary prompts from untrusted
// clients would bypass attestation. If implemented, it should be gated by an
// explicit --allow_adhoc_policies flag and documented as a trust trade-off.
class PolicyRegistry {
 public:
  PolicyRegistry();

  // Load all *.json policy files from a directory.
  //
  // FAIL-FAST: Returns an error (and the server MUST crash) if:
  //   1. The directory does not exist or cannot be opened.
  //   2. Any JSON file is malformed (parse error).
  //   3. Any JSON file is missing required fields (policy_class,
  //      prompt_template, input_schema).
  //   4. Two files define the same policy_class (name collision).
  //
  // On success, all policies from the directory are registered and available
  // via GetPolicy().
  absl::Status LoadFromDirectory(const std::string& dir_path);

  // Returns the definition for a known policy class, or NOT_FOUND.
  absl::StatusOr<const PolicyDefinition*> GetPolicy(
      const std::string& policy_class) const;

  // Returns the number of registered policies.
  int size() const { return policies_.size(); }

 private:
  absl::flat_hash_map<std::string, PolicyDefinition> policies_;
};

}  // namespace ztab

#endif  // ZTAB_SERVER_POLICY_REGISTRY_H_
