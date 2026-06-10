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

#ifndef ZTAB_SERVER_TLS_CERT_GENERATOR_H_
#define ZTAB_SERVER_TLS_CERT_GENERATOR_H_

#include <openssl/evp.h>

#include <memory>
#include <string>
#include <utility>

#include "absl/status/statusor.h"

namespace ztab {

// Generates ephemeral credentials for RA-TLS.
// Flow:
// 1. Instantiate the generator.
// 2. Call GenerateKeyAndGetHash() to generate the EC key and get the SHA-256
//    hash of the public key (to be used as the nonce for attestation).
// 3. Fetch/create the attestation token using the hash.
// 4. Call GenerateCertificate(token) to sign the certificate with the token
//    embedded in a custom X.509 extension, and return {cert_pem, key_pem}.
class EphemeralCredentialGenerator {
 public:
  EphemeralCredentialGenerator();
  ~EphemeralCredentialGenerator();

  // Generates the EC key pair and returns the SHA-256 hash of the public key
  // (raw bytes).
  absl::StatusOr<std::string> GenerateKeyAndGetHash();

  // Signs the certificate with the embedded token and returns
  // {cert_pem, private_key_pem}.
  absl::StatusOr<std::pair<std::string, std::string>> GenerateCertificate(
      const std::string& attestation_token);

  // RAII helper for OpenSSL resource management. Public so anonymous-namespace
  // type aliases in the .cc file can use it.
  template <typename T, void (*FreeFunc)(T*)>
  struct OpenSSLDeleter {
    void operator()(T* ptr) const {
      if (ptr != nullptr) {
        FreeFunc(ptr);
      }
    }
  };

 private:
  using EVP_PKEY_ptr =
      std::unique_ptr<EVP_PKEY, OpenSSLDeleter<EVP_PKEY, EVP_PKEY_free>>;

  EVP_PKEY_ptr pkey_;
};

}  // namespace ztab

#endif  // ZTAB_SERVER_TLS_CERT_GENERATOR_H_
