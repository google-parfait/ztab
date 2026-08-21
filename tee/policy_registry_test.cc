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
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
// implied. See the License for the specific language governing
// permissions and limitations under the License.

#include "policy_registry.h"

#include <fstream>
#include <string>

#include "absl/status/status.h"
#include "gtest/gtest.h"

namespace ztab {
namespace {

// Helper: create a temporary directory with policy JSON files.
class PolicyRegistryTest : public ::testing::Test {
 protected:
  void SetUp() override {
    // Create a unique temp directory.
    char tmpl[] = "/tmp/ztab_policy_test_XXXXXX";
    test_dir_ = mkdtemp(tmpl);
    ASSERT_FALSE(test_dir_.empty());
  }

  void TearDown() override {
    // Clean up temp files.
    if (!test_dir_.empty()) {
      // Remove all files in the directory.
      std::string cmd = "rm -rf " + test_dir_;
      system(cmd.c_str());
    }
  }

  void WriteFile(const std::string& filename, const std::string& content) {
    std::string path = test_dir_ + "/" + filename;
    std::ofstream f(path);
    ASSERT_TRUE(f.is_open()) << "Cannot create " << path;
    f << content;
    f.close();
  }

  std::string test_dir_;
};

// --- LoadFromDirectory ---

TEST_F(PolicyRegistryTest, LoadValidPolicyFile) {
  WriteFile("test_policy.json", R"({
    "policy_class": "TestPolicy",
    "prompt_template": "Hello {inputs}",
    "input_schema": {"type": "object"}
  })");

  PolicyRegistry reg;
  auto status = reg.LoadFromDirectory(test_dir_);
  ASSERT_TRUE(status.ok()) << status;
  EXPECT_EQ(reg.size(), 1);

  auto policy_or = reg.GetPolicy("TestPolicy");
  ASSERT_TRUE(policy_or.ok());
  EXPECT_EQ((*policy_or)->prompt_template, "Hello {inputs}");
}

TEST_F(PolicyRegistryTest, LoadMultiplePolicies) {
  WriteFile("a.json", R"({
    "policy_class": "Alpha",
    "prompt_template": "alpha {inputs}",
    "input_schema": {"type": "object"}
  })");
  WriteFile("b.json", R"({
    "policy_class": "Beta",
    "prompt_template": "beta {inputs}",
    "input_schema": {"type": "object"}
  })");

  PolicyRegistry reg;
  ASSERT_TRUE(reg.LoadFromDirectory(test_dir_).ok());
  EXPECT_EQ(reg.size(), 2);
}

TEST_F(PolicyRegistryTest, LoadWithOutputSchema) {
  WriteFile("p.json", R"({
    "policy_class": "WithOutput",
    "prompt_template": "t {inputs}",
    "input_schema": {"type": "object"},
    "output_schema": {"type": "object", "properties": {}}
  })");

  PolicyRegistry reg;
  ASSERT_TRUE(reg.LoadFromDirectory(test_dir_).ok());
  auto p = reg.GetPolicy("WithOutput");
  ASSERT_TRUE(p.ok());
  EXPECT_FALSE((*p)->default_output_schema_json.empty());
}

// --- Fail-fast error paths ---

TEST_F(PolicyRegistryTest, DirectoryNotFound) {
  PolicyRegistry reg;
  auto status = reg.LoadFromDirectory("/nonexistent/path");
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kNotFound);
}

TEST_F(PolicyRegistryTest, EmptyDirectory) {
  // No .json files -> error.
  PolicyRegistry reg;
  auto status = reg.LoadFromDirectory(test_dir_);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kNotFound);
}

TEST_F(PolicyRegistryTest, MalformedJson) {
  WriteFile("bad.json", "not valid json {{{");
  PolicyRegistry reg;
  auto status = reg.LoadFromDirectory(test_dir_);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kInvalidArgument);
}

TEST_F(PolicyRegistryTest, MissingPolicyClass) {
  WriteFile("no_class.json", R"({
    "prompt_template": "hello",
    "input_schema": {"type": "object"}
  })");
  PolicyRegistry reg;
  auto status = reg.LoadFromDirectory(test_dir_);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kInvalidArgument);
}

TEST_F(PolicyRegistryTest, MissingPromptTemplate) {
  WriteFile("no_template.json", R"({
    "policy_class": "NoTemplate",
    "input_schema": {"type": "object"}
  })");
  PolicyRegistry reg;
  auto status = reg.LoadFromDirectory(test_dir_);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kInvalidArgument);
}

TEST_F(PolicyRegistryTest, MissingInputSchema) {
  WriteFile("no_schema.json", R"({
    "policy_class": "NoSchema",
    "prompt_template": "hello"
  })");
  PolicyRegistry reg;
  auto status = reg.LoadFromDirectory(test_dir_);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kInvalidArgument);
}

TEST_F(PolicyRegistryTest, DuplicatePolicyClass) {
  WriteFile("a.json", R"({
    "policy_class": "Dup",
    "prompt_template": "t1",
    "input_schema": {"type": "object"}
  })");
  WriteFile("b.json", R"({
    "policy_class": "Dup",
    "prompt_template": "t2",
    "input_schema": {"type": "object"}
  })");
  PolicyRegistry reg;
  auto status = reg.LoadFromDirectory(test_dir_);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kAlreadyExists);
}

TEST_F(PolicyRegistryTest, SkipsNonJsonFiles) {
  WriteFile("readme.txt", "not a policy");
  WriteFile("valid.json", R"({
    "policy_class": "Valid",
    "prompt_template": "t",
    "input_schema": {"type": "object"}
  })");
  PolicyRegistry reg;
  ASSERT_TRUE(reg.LoadFromDirectory(test_dir_).ok());
  EXPECT_EQ(reg.size(), 1);
}

// --- GetPolicy ---

TEST_F(PolicyRegistryTest, GetPolicyNotFound) {
  PolicyRegistry reg;
  // No policies loaded.
  auto result = reg.GetPolicy("NonExistent");
  EXPECT_FALSE(result.ok());
  EXPECT_EQ(result.status().code(), absl::StatusCode::kNotFound);
}

TEST_F(PolicyRegistryTest, GetPolicyListsAvailable) {
  PolicyRegistry reg;
  PolicyDefinition def;
  def.prompt_template = "t";
  reg.RegisterPolicy("Alpha", def);
  reg.RegisterPolicy("Beta", def);

  auto result = reg.GetPolicy("Unknown");
  EXPECT_FALSE(result.ok());
  // Error message should list available policies.
  std::string msg(result.status().message());
  EXPECT_NE(msg.find("Alpha"), std::string::npos);
  EXPECT_NE(msg.find("Beta"), std::string::npos);
}

// --- RegisterPolicy (programmatic) ---

TEST_F(PolicyRegistryTest, RegisterPolicyWorks) {
  PolicyRegistry reg;
  PolicyDefinition def;
  def.prompt_template = "test {inputs}";
  reg.RegisterPolicy("Manual", def);
  EXPECT_EQ(reg.size(), 1);

  auto p = reg.GetPolicy("Manual");
  ASSERT_TRUE(p.ok());
  EXPECT_EQ((*p)->prompt_template, "test {inputs}");
}

}  // namespace
}  // namespace ztab
