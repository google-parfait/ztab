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

#include <dirent.h>

#include <fstream>
#include <sstream>
#include <string>

#include "absl/log/log.h"
#include "absl/status/status.h"
#include "absl/strings/str_cat.h"
#include "nlohmann/json.hpp"

namespace ztab {

PolicyRegistry::PolicyRegistry() {
  // Empty — policies are loaded dynamically via LoadFromDirectory().
}

absl::Status PolicyRegistry::LoadFromDirectory(const std::string& dir_path) {
  DIR* dir = opendir(dir_path.c_str());
  if (dir == nullptr) {
    return absl::NotFoundError(
        absl::StrCat("Policy directory not found: '", dir_path, "'"));
  }

  int loaded = 0;
  struct dirent* entry;
  while ((entry = readdir(dir)) != nullptr) {
    std::string filename(entry->d_name);

    // Only process *.json files.
    if (filename.size() < 5 ||
        filename.substr(filename.size() - 5) != ".json") {
      continue;
    }

    std::string filepath = absl::StrCat(dir_path, "/", filename);

    // Read file contents.
    std::ifstream file(filepath);
    if (!file.is_open()) {
      closedir(dir);
      return absl::InternalError(
          absl::StrCat("Cannot open policy file: '", filepath, "'"));
    }

    std::ostringstream contents;
    contents << file.rdbuf();
    file.close();

    // Parse JSON.
    nlohmann::json doc;
    try {
      doc = nlohmann::json::parse(contents.str());
    } catch (const nlohmann::json::parse_error& e) {
      closedir(dir);
      return absl::InvalidArgumentError(
          absl::StrCat("Malformed JSON in '", filepath, "': ", e.what()));
    }

    // Validate required fields.
    if (!doc.contains("policy_class") || !doc["policy_class"].is_string()) {
      closedir(dir);
      return absl::InvalidArgumentError(absl::StrCat(
          "Missing or invalid 'policy_class' in '", filepath, "'"));
    }
    if (!doc.contains("prompt_template") ||
        !doc["prompt_template"].is_string()) {
      closedir(dir);
      return absl::InvalidArgumentError(absl::StrCat(
          "Missing or invalid 'prompt_template' in '", filepath, "'"));
    }
    if (!doc.contains("input_schema") || !doc["input_schema"].is_object()) {
      closedir(dir);
      return absl::InvalidArgumentError(absl::StrCat(
          "Missing or invalid 'input_schema' in '", filepath, "'"));
    }

    std::string policy_class = doc["policy_class"].get<std::string>();

    // Check for name collision.
    if (policies_.contains(policy_class)) {
      closedir(dir);
      return absl::AlreadyExistsError(
          absl::StrCat("Duplicate policy_class '", policy_class, "' found in '",
                       filepath, "'. Each policy_class must be unique."));
    }

    // Build PolicyDefinition.
    PolicyDefinition def;
    def.prompt_template = doc["prompt_template"].get<std::string>();
    def.default_input_schema_json = doc["input_schema"].dump();

    if (doc.contains("output_schema") && doc["output_schema"].is_object()) {
      def.default_output_schema_json = doc["output_schema"].dump();
    }

    policies_[policy_class] = std::move(def);
    ++loaded;
    LOG(INFO) << "Loaded policy '" << policy_class << "' from " << filepath;
  }

  closedir(dir);

  if (loaded == 0) {
    return absl::NotFoundError(
        absl::StrCat("No *.json policy files found in '", dir_path, "'"));
  }

  LOG(INFO) << "PolicyRegistry: loaded " << loaded << " policies from "
            << dir_path;
  return absl::OkStatus();
}

absl::StatusOr<const PolicyDefinition*> PolicyRegistry::GetPolicy(
    const std::string& policy_class) const {
  auto it = policies_.find(policy_class);
  if (it == policies_.end()) {
    std::string available;
    for (const auto& [name, _] : policies_) {
      if (!available.empty()) available += ", ";
      available += name;
    }
    return absl::NotFoundError(absl::StrCat(
        "Unknown policy class: '", policy_class,
        "'. Available: ", available.empty() ? "(none)" : available));
  }
  return &it->second;
}

}  // namespace ztab
