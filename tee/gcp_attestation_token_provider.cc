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

#include "gcp_attestation_token_provider.h"

#include <memory>
#include <string>

#include "absl/log/log.h"
#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/strings/str_cat.h"
#include "absl/strings/string_view.h"
#include "http_client.h"

namespace ztab {
namespace {

// The Unix domain socket exposed by the Confidential Space attestation agent.
constexpr absl::string_view kAgentSocketPath =
    "/run/container_launcher/teeserver.sock";

// The ITA token endpoint on the Confidential Space agent.
constexpr absl::string_view kItaTokenUrl = "http://localhost/v1/intel/token";

// The audience claim for ZTAB attestation tokens.
constexpr absl::string_view kAudience = "ztab_tls";

class GcpAttestationTokenProvider : public AttestationTokenProvider {
 public:
  absl::StatusOr<std::string> GetAttestationToken(
      absl::string_view nonce) override {
    // Build the JSON payload for the ITA token request.
    // The nonce is the Base64URL-encoded SHA-256 hash of the server's
    // ephemeral public key, binding the attestation to the TLS credential.
    std::string json_payload = absl::StrCat(
        "{\"audience\": \"", kAudience,
        "\", \"token_type\": \"PRINCIPAL_TAGS\", \"nonces\": [\"", nonce,
        "\"]}");

    LOG(INFO) << "Requesting ITA attestation token from Confidential Space "
                 "agent at "
              << kAgentSocketPath;

    absl::StatusOr<std::string> response =
        PostJsonViaUnixSocket(kItaTokenUrl, kAgentSocketPath, json_payload);

    if (!response.ok()) {
      return absl::Status(
          response.status().code(),
          absl::StrCat("Failed to fetch ITA attestation token: ",
                       response.status().message()));
    }

    LOG(INFO) << "Received ITA attestation token ("
              << response->length() << " bytes).";
    return *response;
  }
};

}  // namespace

std::unique_ptr<AttestationTokenProvider>
CreateGcpAttestationTokenProvider() {
  return std::make_unique<GcpAttestationTokenProvider>();
}

}  // namespace ztab
