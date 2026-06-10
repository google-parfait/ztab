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

#include <iostream>
#include <memory>
#include <string>
#include <utility>

#include "absl/log/initialize.h"
#include "absl/log/log.h"
#include "absl/status/statusor.h"
#include "absl/strings/str_cat.h"
#include "grpcpp/grpcpp.h"
#include "grpcpp/security/server_credentials.h"
#include "session_manager.grpc.pb.h"
#include "tls_cert_generator.h"

namespace ztab {
namespace {

// Minimal Base64Url encoder (no padding).
std::string Base64UrlEncode(const std::string& input) {
  static const char alphabet[] =
      "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
  std::string result;
  result.reserve((input.size() + 2) / 3 * 4);
  int val = 0;
  int valb = -6;
  for (unsigned char c : input) {
    val = (val << 8) + c;
    valb += 8;
    while (valb >= 0) {
      result.push_back(alphabet[(val >> valb) & 0x3F]);
      valb -= 6;
    }
  }
  if (valb > -6) {
    result.push_back(alphabet[((val << 8) >> (valb + 8)) & 0x3F]);
  }
  return result;
}

// Generates a mock attestation token (unsigned JWT) that binds the given
// public key hash via the eat_nonce claim.  In production this is fetched
// from the GCP Confidential Computing metadata endpoint.
std::string GenerateMockAttestationToken(const std::string& key_hash) {
  // Header: {"alg":"none","typ":"JWT"}
  const std::string header_b64 = "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0";

  std::string nonce_b64 = Base64UrlEncode(key_hash);

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
      nonce_b64,
      "\"\n"
      "}");

  std::string payload_b64 = Base64UrlEncode(payload_json);

  // JWT = header.payload.signature  (signature empty for alg=none)
  return absl::StrCat(header_b64, ".", payload_b64, ".");
}

// Trivial Echo implementation.
class AgentBrokerServiceImpl final : public AgentBrokerService::Service {
 public:
  grpc::Status Echo(grpc::ServerContext* context, const EchoRequest* request,
                    EchoResponse* response) override {
    LOG(INFO) << "Received Echo request: " << request->message();
    response->set_message(absl::StrCat("Echo: ", request->message()));
    return grpc::Status::OK;
  }
};

void RunServer(const std::string& port) {
  absl::InitializeLog();

  LOG(INFO) << "Generating ephemeral RA-TLS credentials...";

  // 1. Generate ephemeral key and get the public key hash.
  EphemeralCredentialGenerator generator;
  absl::StatusOr<std::string> hash_or = generator.GenerateKeyAndGetHash();
  if (!hash_or.ok()) {
    LOG(ERROR) << "Failed to generate key: " << hash_or.status().ToString();
    return;
  }
  std::string key_hash = *hash_or;

  // 2. Create a mock attestation token binding this key.
  std::string attestation_token = GenerateMockAttestationToken(key_hash);
  LOG(INFO) << "Mock attestation token generated (" << attestation_token.size()
            << " bytes).";

  // 3. Generate the self-signed certificate with the token embedded.
  absl::StatusOr<std::pair<std::string, std::string>> creds_or =
      generator.GenerateCertificate(attestation_token);
  if (!creds_or.ok()) {
    LOG(ERROR) << "Failed to generate certificate: "
               << creds_or.status().ToString();
    return;
  }
  std::string cert_pem = creds_or->first;
  std::string private_key_pem = creds_or->second;

  LOG(INFO) << "Ephemeral certificate ready.";

  // 4. Configure gRPC server with TLS credentials.
  grpc::SslServerCredentialsOptions ssl_opts;
  grpc::SslServerCredentialsOptions::PemKeyCertPair pkcp = {private_key_pem,
                                                            cert_pem};
  ssl_opts.pem_key_cert_pairs.push_back(pkcp);
  ssl_opts.client_certificate_request =
      GRPC_SSL_DONT_REQUEST_CLIENT_CERTIFICATE;

  auto server_creds = grpc::SslServerCredentials(ssl_opts);

  // 5. Start the server.
  AgentBrokerServiceImpl service;
  std::string server_address = absl::StrCat("0.0.0.0:", port);

  grpc::ServerBuilder builder;
  builder.AddListeningPort(server_address, server_creds);
  builder.RegisterService(&service);

  std::unique_ptr<grpc::Server> server(builder.BuildAndStart());
  LOG(INFO) << "ZTAB Server listening on " << server_address;

  server->Wait();
}

}  // namespace
}  // namespace ztab

int main(int argc, char** argv) {
  std::string port = "8000";
  if (argc > 1) {
    port = argv[1];
  }
  ztab::RunServer(port);
  return 0;
}
