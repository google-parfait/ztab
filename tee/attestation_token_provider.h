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

#ifndef ZTAB_SERVER_ATTESTATION_TOKEN_PROVIDER_H_
#define ZTAB_SERVER_ATTESTATION_TOKEN_PROVIDER_H_

#include <string>

#include "absl/status/statusor.h"
#include "absl/strings/string_view.h"

namespace ztab {

// Abstract interface for obtaining an attestation token.
//
// Implementations provide either a mock token (for local development) or a
// real token from the GCP Confidential Space agent (for production TEE).
//
// Usage:
//   1. Generate an ephemeral key pair and compute its public key hash.
//   2. Base64URL-encode the hash to produce the nonce.
//   3. Call GetAttestationToken(nonce) to obtain a JWT whose eat_nonce claim
//      binds the token to the key pair.
//   4. Embed the JWT in the server's TLS certificate.
class AttestationTokenProvider {
 public:
  virtual ~AttestationTokenProvider() = default;

  // Returns an attestation token (JWT) incorporating the given nonce.
  // The nonce is typically the Base64URL-encoded SHA-256 hash of the server's
  // ephemeral TLS public key.
  virtual absl::StatusOr<std::string> GetAttestationToken(
      absl::string_view nonce) = 0;
};

}  // namespace ztab

#endif  // ZTAB_SERVER_ATTESTATION_TOKEN_PROVIDER_H_
