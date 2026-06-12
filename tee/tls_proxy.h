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

#ifndef ZTAB_SERVER_TLS_PROXY_H_
#define ZTAB_SERVER_TLS_PROXY_H_

#include <atomic>
#include <condition_variable>
#include <future>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <vector>

#include "absl/status/status.h"
#include "attestation_token_provider.h"
#include "openssl/ssl.h"

namespace ztab {

// A standard TLS-terminating reverse proxy implementing the RA-TLS pattern
// (Remote Attestation over TLS), analogous to nginx/envoy TLS termination.
//
// Standards used (no bespoke cryptography):
//   - TLS 1.3 via BoringSSL (RFC 8446)
//   - X.509v3 self-signed certificates (RFC 5280)
//   - NIST P-256 / secp256r1 ephemeral keys (FIPS 186-4)
//   - SHA-256 key hashing for EAT nonce binding (RFC 9334 RATS)
//   - HTTP/2 ALPN negotiation (RFC 7301) for gRPC compatibility
//
// For each incoming connection:
// 1. Fetches a fresh attestation token with a public-key-bound nonce.
// 2. Generates a fresh self-signed certificate with the token embedded.
// 3. Performs TLS handshake with the client.
// 4. Pipes the decrypted traffic to a local insecure gRPC server on localhost.
class TlsProxy {
 public:
  TlsProxy(int public_port, int local_port,
           AttestationTokenProvider* attestation_provider);
  ~TlsProxy();

  // Starts the proxy server in a background thread.
  absl::Status Start();

  // Stops the proxy server and joins the listener thread.
  void Stop();

 private:
  void ListenLoop();
  void HandleConnection(int client_fd);
  void HandleConnectionImpl(int client_fd);
  // Returns a cached SSL_CTX, rebuilding it only when the credential cache
  // expires. This avoids the overhead of creating a new SSL_CTX per connection.
  // The caller must free the returned context via SSL_CTX_free.
  SSL_CTX* GetOrCreateSSLCtx();

  int public_port_;
  int local_port_;
  AttestationTokenProvider* attestation_provider_;  // Not owned.

  std::atomic<bool> shutdown_{false};
  std::atomic<int> active_connections_{0};
  std::mutex connection_throttle_mu_;
  std::condition_variable connection_throttle_cv_;
  static constexpr int kMaxConcurrentConnections = 50;
  int listen_fd_ = -1;
  std::thread listener_thread_;

  std::mutex futures_mu_;
  std::vector<std::future<void>> futures_;

  std::mutex active_sockets_mu_;
  std::set<int> active_sockets_;

  void AddActiveSocket(int fd);
  void RemoveActiveSocket(int fd);

  // 10-second lazy cache to handle client pre-flight cert fetch followed
  // immediately by gRPC connection.
  std::mutex cache_mu_;
  absl::Time cached_time_ = absl::UnixEpoch();
  std::string cached_cert_pem_;
  std::string cached_key_pem_;
  SSL_CTX* cached_ssl_ctx_ = nullptr;  // Rebuilt when credentials change.
};

}  // namespace ztab

#endif  // ZTAB_SERVER_TLS_PROXY_H_
