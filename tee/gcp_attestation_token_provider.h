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

#ifndef ZTAB_SERVER_GCP_ATTESTATION_TOKEN_PROVIDER_H_
#define ZTAB_SERVER_GCP_ATTESTATION_TOKEN_PROVIDER_H_

#include <memory>

#include "attestation_token_provider.h"

namespace ztab {

// Creates an attestation token provider that fetches a real ITA (Intel Trust
// Authority) attestation token from the GCP Confidential Space agent via its
// local Unix domain socket.
//
// This provider is for production use inside a Confidential Space TEE. It
// communicates with the agent at /run/container_launcher/teeserver.sock.
std::unique_ptr<AttestationTokenProvider>
CreateGcpAttestationTokenProvider();

}  // namespace ztab

#endif  // ZTAB_SERVER_GCP_ATTESTATION_TOKEN_PROVIDER_H_
