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

#include "mock_attestation_token_provider.h"

#include <string>

#include "absl/status/statusor.h"
#include "encoding_utils.h"
#include "gtest/gtest.h"

namespace ztab {
namespace {

TEST(MockAttestationTest, ReturnsJwtWithThreeParts) {
  auto provider = CreateMockAttestationTokenProvider();
  auto token_or = provider->GetAttestationToken("test-nonce");
  ASSERT_TRUE(token_or.ok()) << token_or.status();

  const std::string& token = *token_or;

  // JWT has three dot-separated parts: header.payload.signature
  int dot_count = 0;
  for (char c : token) {
    if (c == '.') ++dot_count;
  }
  EXPECT_EQ(dot_count, 2) << "JWT must have exactly 2 dots";
}

TEST(MockAttestationTest, HeaderIsAlgNone) {
  auto provider = CreateMockAttestationTokenProvider();
  auto token_or = provider->GetAttestationToken("nonce");
  ASSERT_TRUE(token_or.ok());

  const std::string& token = *token_or;
  // Header is the first segment before the first dot.
  std::string header = token.substr(0, token.find('.'));
  // Expected: base64url of {"alg":"none","typ":"JWT"}
  EXPECT_EQ(header, "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0");
}

TEST(MockAttestationTest, EmptySignature) {
  auto provider = CreateMockAttestationTokenProvider();
  auto token_or = provider->GetAttestationToken("nonce");
  ASSERT_TRUE(token_or.ok());

  const std::string& token = *token_or;
  // For alg=none, signature (after last dot) must be empty.
  size_t last_dot = token.rfind('.');
  ASSERT_NE(last_dot, std::string::npos);
  EXPECT_EQ(token.size(), last_dot + 1)
      << "Signature must be empty for alg=none";
}

TEST(MockAttestationTest, PayloadContainsNonce) {
  const std::string nonce = "my-test-nonce-value";
  auto provider = CreateMockAttestationTokenProvider();
  auto token_or = provider->GetAttestationToken(nonce);
  ASSERT_TRUE(token_or.ok());

  const std::string& token = *token_or;

  // Extract payload (between first and second dot).
  size_t first_dot = token.find('.');
  size_t second_dot = token.find('.', first_dot + 1);
  std::string payload_b64 =
      token.substr(first_dot + 1, second_dot - first_dot - 1);

  // Decode base64url payload.
  std::string payload;
  // Add padding for absl decode.
  while (payload_b64.size() % 4 != 0) payload_b64 += '=';
  ASSERT_TRUE(absl::WebSafeBase64Unescape(payload_b64, &payload));

  // The payload JSON must contain the nonce.
  EXPECT_NE(payload.find(nonce), std::string::npos)
      << "Nonce not found in JWT payload";
  // Must contain expected issuer.
  EXPECT_NE(payload.find("confidentialcomputing.googleapis.com"),
            std::string::npos);
  // Must contain expected audience.
  EXPECT_NE(payload.find("ztab_tls"), std::string::npos);
}

TEST(MockAttestationTest, DifferentNoncesProduceDifferentTokens) {
  auto provider = CreateMockAttestationTokenProvider();
  auto t1 = provider->GetAttestationToken("nonce-1");
  auto t2 = provider->GetAttestationToken("nonce-2");
  ASSERT_TRUE(t1.ok());
  ASSERT_TRUE(t2.ok());
  EXPECT_NE(*t1, *t2);
}

}  // namespace
}  // namespace ztab
