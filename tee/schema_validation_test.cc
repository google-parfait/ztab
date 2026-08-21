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

// Unit tests for ValidateJsonSchema and StripMarkdownCodeFences.
// These are critical security functions in session_manager.cc that
// validate untrusted input against policy schemas and strip LLM
// code fence artifacts.

#include <string>

#include "absl/status/status.h"
#include "gtest/gtest.h"
#include "policy_registry.h"
#include "session_manager.h"

namespace ztab {
namespace {

// =============================================================
// Test Suite: ValidateJsonSchema
// =============================================================

TEST(ValidateJsonSchemaTest, ValidObjectMatchesSchema) {
  std::string schema = R"({
    "type": "object",
    "properties": {
      "name": {"type": "string"},
      "age": {"type": "integer"}
    },
    "required": ["name"]
  })";
  std::string value = R"({"name": "Alice", "age": 30})";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);
  auto status = mgr.ValidateJsonSchema(value, schema);
  EXPECT_TRUE(status.ok()) << status;
}

TEST(ValidateJsonSchemaTest, MissingRequiredField) {
  std::string schema = R"({
    "type": "object",
    "properties": {
      "name": {"type": "string"}
    },
    "required": ["name"]
  })";
  std::string value = R"({"age": 30})";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);
  auto status = mgr.ValidateJsonSchema(value, schema);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kInvalidArgument);
}

TEST(ValidateJsonSchemaTest, WrongType) {
  std::string schema = R"({"type": "string"})";
  std::string value = "42";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);
  auto status = mgr.ValidateJsonSchema(value, schema);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kInvalidArgument);
}

TEST(ValidateJsonSchemaTest, StringMatchesPattern) {
  std::string schema = R"({
    "type": "string",
    "pattern": "^[A-Z]{3}$"
  })";
  std::string value = R"("ABC")";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);
  EXPECT_TRUE(mgr.ValidateJsonSchema(value, schema).ok());
}

TEST(ValidateJsonSchemaTest, StringFailsPattern) {
  std::string schema = R"({
    "type": "string",
    "pattern": "^[A-Z]{3}$"
  })";
  std::string value = R"("abc")";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);
  auto status = mgr.ValidateJsonSchema(value, schema);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kInvalidArgument);
}

TEST(ValidateJsonSchemaTest, ArrayBounds) {
  std::string schema = R"({
    "type": "array",
    "minItems": 2,
    "maxItems": 5,
    "items": {"type": "string"}
  })";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);

  // Too few.
  EXPECT_FALSE(mgr.ValidateJsonSchema(R"(["a"])", schema).ok());

  // Just right.
  EXPECT_TRUE(mgr.ValidateJsonSchema(R"(["a", "b"])", schema).ok());

  // Too many.
  EXPECT_FALSE(
      mgr.ValidateJsonSchema(R"(["a","b","c","d","e","f"])", schema).ok());
}

TEST(ValidateJsonSchemaTest, AdditionalPropertiesFalse) {
  std::string schema = R"({
    "type": "object",
    "properties": {
      "name": {"type": "string"}
    },
    "additionalProperties": false
  })";
  std::string value = R"({"name": "Bob", "extra": 1})";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);
  auto status = mgr.ValidateJsonSchema(value, schema);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kInvalidArgument);
}

TEST(ValidateJsonSchemaTest, NestedObjectValidation) {
  std::string schema = R"({
    "type": "object",
    "properties": {
      "address": {
        "type": "object",
        "properties": {
          "city": {"type": "string"}
        },
        "required": ["city"]
      }
    }
  })";
  // Missing required nested field.
  std::string value = R"({"address": {}})";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);
  auto status = mgr.ValidateJsonSchema(value, schema);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kInvalidArgument);
}

TEST(ValidateJsonSchemaTest, BooleanType) {
  std::string schema = R"({"type": "boolean"})";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);

  EXPECT_TRUE(mgr.ValidateJsonSchema("true", schema).ok());
  EXPECT_TRUE(mgr.ValidateJsonSchema("false", schema).ok());
  EXPECT_FALSE(mgr.ValidateJsonSchema("1", schema).ok());
  EXPECT_FALSE(mgr.ValidateJsonSchema(R"("true")", schema).ok());
}

TEST(ValidateJsonSchemaTest, NumberType) {
  std::string schema = R"({"type": "number"})";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);

  EXPECT_TRUE(mgr.ValidateJsonSchema("3.14", schema).ok());
  EXPECT_TRUE(mgr.ValidateJsonSchema("42", schema).ok());
  EXPECT_FALSE(mgr.ValidateJsonSchema(R"("42")", schema).ok());
}

TEST(ValidateJsonSchemaTest, IntegerType) {
  std::string schema = R"({"type": "integer"})";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);

  EXPECT_TRUE(mgr.ValidateJsonSchema("42", schema).ok());
  EXPECT_FALSE(mgr.ValidateJsonSchema("3.14", schema).ok());
}

TEST(ValidateJsonSchemaTest, MalformedJsonRejected) {
  std::string schema = R"({"type": "object"})";
  std::string value = "not valid json {{{";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);
  auto status = mgr.ValidateJsonSchema(value, schema);
  EXPECT_FALSE(status.ok());
  EXPECT_EQ(status.code(), absl::StatusCode::kInvalidArgument);
}

TEST(ValidateJsonSchemaTest, EmptySchemaAcceptsAnything) {
  std::string schema = "{}";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);

  EXPECT_TRUE(mgr.ValidateJsonSchema("42", schema).ok());
  EXPECT_TRUE(mgr.ValidateJsonSchema(R"("hello")", schema).ok());
  EXPECT_TRUE(mgr.ValidateJsonSchema("[1,2,3]", schema).ok());
}

TEST(ValidateJsonSchemaTest, ArrayItemTypeValidation) {
  std::string schema = R"({
    "type": "array",
    "items": {"type": "integer"}
  })";

  PolicyRegistry reg;
  SessionManager mgr(nullptr, &reg);

  EXPECT_TRUE(mgr.ValidateJsonSchema("[1, 2, 3]", schema).ok());
  EXPECT_FALSE(mgr.ValidateJsonSchema(R"([1, "two", 3])", schema).ok());
}

}  // namespace
}  // namespace ztab
