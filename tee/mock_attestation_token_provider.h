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

#ifndef ZTAB_SERVER_MOCK_ATTESTATION_TOKEN_PROVIDER_H_
#define ZTAB_SERVER_MOCK_ATTESTATION_TOKEN_PROVIDER_H_

#include <memory>

#include "attestation_token_provider.h"

namespace ztab {

// Creates a mock attestation token provider that generates unsigned JWTs
// (alg=none) simulating a GCP Confidential Space attestation report.
//
// WARNING: This provider performs no real attestation and MUST only be used
// for local development and testing.
std::unique_ptr<AttestationTokenProvider> CreateMockAttestationTokenProvider();

}  // namespace ztab

#endif  // ZTAB_SERVER_MOCK_ATTESTATION_TOKEN_PROVIDER_H_
