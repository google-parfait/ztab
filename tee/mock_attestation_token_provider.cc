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

#include "mock_attestation_token_provider.h"

#include <memory>
#include <string>

#include "absl/status/statusor.h"
#include "absl/strings/str_cat.h"
#include "absl/strings/string_view.h"
#include "encoding_utils.h"

namespace ztab {
namespace {


class MockAttestationTokenProvider : public AttestationTokenProvider {
 public:
  absl::StatusOr<std::string> GetAttestationToken(
      absl::string_view nonce) override {
    // Header: {"alg":"none","typ":"JWT"}
    const std::string header_b64 = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0";

    std::string payload_json = absl::StrCat(
        "{\n"
        "  \"iss\": \"https://confidentialcomputing.googleapis.com\",\n"
        "  \"aud\": \"ztab_tls\",\n"
        "  \"dbgstat\": \"disabled-since-boot\",\n"
        "  \"secboot\": true,\n"
        "  \"hwmodel\": \"GCP_INTEL_TDX\",\n"
        "  \"submods\": {\n"
        "    \"container\": {\n"
        "      \"image_digest\": "
        "\"sha256:0000000000000000000000000000000000000000000000000000000000000"
        "000\"\n"
        "    }\n"
        "  },\n"
        "  \"eat_nonce\": \"",
        nonce,
        "\"\n"
        "}");

    std::string payload_b64 = Base64UrlEncode(payload_json);

    // JWT = header.payload.signature  (signature empty for alg=none)
    return absl::StrCat(header_b64, ".", payload_b64, ".");
  }
};

}  // namespace

std::unique_ptr<AttestationTokenProvider>
CreateMockAttestationTokenProvider() {
  return std::make_unique<MockAttestationTokenProvider>();
}

}  // namespace ztab
