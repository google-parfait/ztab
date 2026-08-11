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

#include "tls_proxy.h"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <atomic>
#include <chrono>  // NOLINT
#include <cstdio>
#include <memory>
#include <string>
#include <thread>  // NOLINT
#include <utility>

#include "absl/log/log.h"
#include "absl/status/status.h"
#include "absl/time/clock.h"
#include "absl/time/time.h"
#include "encoding_utils.h"
#include "openssl/bio.h"
#include "openssl/err.h"
#include "openssl/pem.h"
#include "openssl/ssl.h"
#include "openssl/x509.h"
#include "tls_cert_generator.h"

namespace ztab {

TlsProxy::TlsProxy(int public_port, int local_port,
                   AttestationTokenProvider* attestation_provider)
    : public_port_(public_port),
      local_port_(local_port),
      attestation_provider_(attestation_provider) {}

TlsProxy::~TlsProxy() { Stop(); }

void TlsProxy::AddActiveSocket(int fd) {
  if (fd < 0) return;
  std::lock_guard<std::mutex> lock(active_sockets_mu_);
  active_sockets_.insert(fd);
}

void TlsProxy::RemoveActiveSocket(int fd) {
  if (fd < 0) return;
  std::lock_guard<std::mutex> lock(active_sockets_mu_);
  active_sockets_.erase(fd);
}

absl::Status TlsProxy::Start() {
  listen_fd_ = socket(AF_INET, SOCK_STREAM, 0);
  if (listen_fd_ < 0) {
    return absl::InternalError("Failed to create listening socket.");
  }

  int opt = 1;
  if (setsockopt(listen_fd_, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt)) < 0) {
    close(listen_fd_);
    return absl::InternalError("Failed to set SO_REUSEADDR.");
  }

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = INADDR_ANY;
  addr.sin_port = htons(public_port_);

  if (bind(listen_fd_, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
    close(listen_fd_);
    return absl::InternalError("Failed to bind to public port.");
  }

  if (listen(listen_fd_, 128) < 0) {
    close(listen_fd_);
    return absl::InternalError("Failed to listen on public port.");
  }

  LOG(INFO) << "TLS Proxy listening on 0.0.0.0:" << public_port_
            << ", forwarding to 127.0.0.1:" << local_port_;

  listener_thread_ = std::thread(&TlsProxy::ListenLoop, this);
  return absl::OkStatus();
}

void TlsProxy::Stop() {
  if (shutdown_.exchange(true)) return;

  if (listen_fd_ != -1) {
    // shutdown and close to break the accept() block in the listener thread
    shutdown(listen_fd_, SHUT_RDWR);
    close(listen_fd_);
  }

  if (listener_thread_.joinable()) {
    listener_thread_.join();
  }

  // Interrupt all active connections so threads can exit
  {
    std::lock_guard<std::mutex> lock(active_sockets_mu_);
    for (int fd : active_sockets_) {
      shutdown(fd, SHUT_RDWR);
    }
  }

  if (active_connections_ > 0) {
    LOG(WARNING) << "TLS Proxy: draining " << active_connections_.load()
                 << " active connection(s)...";
  }

  // Wait for all futures to finish cleanly
  std::vector<std::future<void>> futures;
  {
    std::lock_guard<std::mutex> lock(futures_mu_);
    futures = std::move(futures_);
  }
  for (auto& f : futures) {
    if (f.valid()) f.wait();
  }
  {
    std::lock_guard<std::mutex> lock(cache_mu_);
    if (cached_ssl_ctx_) {
      SSL_CTX_free(cached_ssl_ctx_);
      cached_ssl_ctx_ = nullptr;
    }
  }
}

void TlsProxy::ListenLoop() {
  while (!shutdown_) {
    sockaddr_in client_addr{};
    socklen_t client_len = sizeof(client_addr);
    int client_fd =
        accept(listen_fd_, (struct sockaddr*)&client_addr, &client_len);
    if (client_fd < 0) {
      if (shutdown_) break;
      LOG(ERROR) << "Accept failed: " << strerror(errno);
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
      continue;
    }

    {
      std::unique_lock<std::mutex> throttle_lock(connection_throttle_mu_);
      connection_throttle_cv_.wait(throttle_lock, [this]() {
        return active_connections_.load() < kMaxConcurrentConnections ||
               shutdown_.load();
      });
      if (shutdown_.load()) {
        close(client_fd);
        break;
      }
    }

    auto fut = std::async(std::launch::async, &TlsProxy::HandleConnection, this,
                          client_fd);
    {
      std::lock_guard<std::mutex> lock(futures_mu_);
      futures_.push_back(std::move(fut));
      // Prune finished futures
      futures_.erase(
          std::remove_if(futures_.begin(), futures_.end(),
                         [](const std::future<void>& f) {
                           return f.wait_for(std::chrono::seconds(0)) ==
                                  std::future_status::ready;
                         }),
          futures_.end());
    }
  }
}

// HTTP/2 ALPN selection callback per RFC 7301. Required for gRPC clients,
// which advertise "h2" during the TLS handshake.
static int ALPNSelectCallback(SSL* ssl, const unsigned char** out,
                              unsigned char* outlen, const unsigned char* in,
                              unsigned int inlen, void* arg) {
  unsigned int i = 0;
  while (i < inlen) {
    unsigned int len = in[i];
    if (i + 1 + len > inlen) {
      break;
    }
    if (len == 2 && in[i + 1] == 'h' && in[i + 2] == '2') {
      *out = &in[i + 1];
      *outlen = 2;
      return SSL_TLSEXT_ERR_OK;
    }
    i += 1 + len;
  }
  return SSL_TLSEXT_ERR_NOACK;
}

void TlsProxy::HandleConnection(int client_fd) {
  ++active_connections_;
  AddActiveSocket(client_fd);
  HandleConnectionImpl(client_fd);
  RemoveActiveSocket(client_fd);
  --active_connections_;
  connection_throttle_cv_.notify_one();
}

void TlsProxy::HandleConnectionImpl(int client_fd) {
  LOG(INFO) << "Proxy: Handling connection (fd=" << client_fd << ")";
  absl::Time start_time = absl::Now();

  // Get or re-use the cached SSL_CTX (rebuilt only when credentials rotate).
  SSL_CTX* raw_ctx = GetOrCreateSSLCtx();
  if (raw_ctx == nullptr) {
    LOG(ERROR) << "Proxy: Failed to obtain SSL_CTX, closing connection (fd="
               << client_fd << ")";
    close(client_fd);
    return;
  }
  std::unique_ptr<SSL_CTX, decltype(&SSL_CTX_free)> ctx(raw_ctx, SSL_CTX_free);

  // 1. Perform TLS handshake
  SSL* ssl = SSL_new(ctx.get());
  SSL_set_fd(ssl, client_fd);
  if (SSL_accept(ssl) <= 0) {
    LOG(WARNING) << "Proxy: TLS handshake failed.";
    // Print OpenSSL errors if any
    unsigned long err;  // NOLINT
    while ((err = ERR_get_error()) != 0) {
      char buf[256];
      ERR_error_string_n(err, buf, sizeof(buf));
      LOG(WARNING) << "  OpenSSL Error: " << buf;
    }
    SSL_free(ssl);
    close(client_fd);
    return;
  }

  // 6. Connect to local insecure gRPC server
  int local_fd = socket(AF_INET, SOCK_STREAM, 0);
  if (local_fd < 0) {
    LOG(ERROR) << "Proxy: Failed to create local socket.";
    SSL_free(ssl);
    close(client_fd);
    return;
  }
  AddActiveSocket(local_fd);

  sockaddr_in local_addr{};
  local_addr.sin_family = AF_INET;
  local_addr.sin_addr.s_addr = inet_addr("127.0.0.1");
  local_addr.sin_port = htons(local_port_);

  if (connect(local_fd, (struct sockaddr*)&local_addr, sizeof(local_addr)) <
      0) {
    LOG(ERROR) << "Proxy: Failed to connect to local gRPC on port "
               << local_port_;
    close(local_fd);
    SSL_free(ssl);
    close(client_fd);
    return;
  }

  absl::Duration setup_elapsed = absl::Now() - start_time;
  LOG(INFO) << "Proxy: Connection established and local channel open in "
            << absl::FormatDuration(setup_elapsed) << ". Starting pipe.";

  // 7. Start piping threads.
  // Note on Thread Safety (Design Decision): Concurrent SSL_read and SSL_write
  // on the same SSL* object is technically not thread-safe in standard upstream
  // OpenSSL. However, because we hermetically build and link against Google's
  // BoringSSL (via our gRPC dependency), this full-duplex two-thread proxy
  // pattern is explicitly supported and 100% thread-safe. BoringSSL maintains
  // internal locks specifically for this use case. Do not request a mutex here,
  // as it would unnecessarily block full-duplex multiplexing. One thread for
  // Client -> local gRPC
  std::thread c_to_l([ssl, local_fd]() {
    char buf[8192];
    while (true) {
      int n = SSL_read(ssl, buf, sizeof(buf));
      if (n <= 0) {
        break;
      }
      int sent = 0;
      while (sent < n) {
        int w = write(local_fd, buf + sent, n - sent);
        if (w <= 0) break;
        sent += w;
      }
    }
    // Wake up the other thread
    shutdown(local_fd, SHUT_RDWR);
  });

  // One thread for local gRPC -> Client
  std::thread l_to_c([ssl, local_fd]() {
    char buf[8192];
    while (true) {
      int n = read(local_fd, buf, sizeof(buf));
      if (n <= 0) {
        break;
      }
      int sent = 0;
      while (sent < n) {
        int w = SSL_write(ssl, buf + sent, n - sent);
        if (w <= 0) break;
        sent += w;
      }
    }
    // Wake up the other thread
    shutdown(SSL_get_fd(ssl), SHUT_RDWR);
  });

  c_to_l.join();
  l_to_c.join();

  LOG(INFO) << "Proxy: Connection closed.";

  // 8. Clean up per-connection resources
  SSL_free(ssl);
  RemoveActiveSocket(local_fd);
  close(local_fd);
  close(client_fd);
}

// Implements the RA-TLS credential generation flow (RFC 9334 RATS pattern):
//   1. Generate ephemeral NIST P-256 key pair (FIPS 186-4)
//   2. SHA-256 hash the SubjectPublicKeyInfo DER (RFC 5280)
//   3. Base64URL-encode the hash as the EAT nonce (RFC 4648 §5)
//   4. Request attestation token bound to this nonce from the TEE agent
//   5. Embed token in X.509v3 certificate extension, sign with ephemeral key
SSL_CTX* TlsProxy::GetOrCreateSSLCtx() {
  std::lock_guard<std::mutex> lock(cache_mu_);
  absl::Duration age = absl::Now() - cached_time_;
  if (age < absl::Seconds(10) && !cached_cert_pem_.empty()) {
    LOG(INFO) << "Proxy: Serving cached credentials (age: "
              << absl::FormatDuration(age) << ")";
    if (cached_ssl_ctx_) SSL_CTX_up_ref(cached_ssl_ctx_);
    return cached_ssl_ctx_;
  }

  LOG(INFO) << "Proxy: Cache expired or empty. Generating fresh credentials...";
  EphemeralCredentialGenerator generator;
  auto hash_or = generator.GenerateKeyAndGetHash();
  if (!hash_or.ok()) {
    LOG(ERROR) << "Proxy: Key generation failed: "
               << hash_or.status().ToString();
    return nullptr;
  }
  std::string nonce = Base64UrlEncode(*hash_or);

  auto token_or = attestation_provider_->GetAttestationToken(nonce);
  if (!token_or.ok()) {
    LOG(ERROR) << "Proxy: Failed to get attestation token: "
               << token_or.status().ToString();
    return nullptr;
  }

  auto cert_or = generator.GenerateCertificate(*token_or);
  if (!cert_or.ok()) {
    LOG(ERROR) << "Proxy: Cert generation failed: "
               << cert_or.status().ToString();
    return nullptr;
  }

  cached_cert_pem_ = cert_or->first;
  cached_key_pem_ = cert_or->second;
  cached_time_ = absl::Now();

  // Rebuild the SSL_CTX with the new credentials.
  if (cached_ssl_ctx_) {
    SSL_CTX_free(cached_ssl_ctx_);
    cached_ssl_ctx_ = nullptr;
  }
  cached_ssl_ctx_ = SSL_CTX_new(TLS_server_method());
  if (cached_ssl_ctx_ == nullptr) {
    LOG(ERROR) << "Proxy: Failed to create SSL_CTX.";
    return nullptr;
  }
  SSL_CTX_set_min_proto_version(cached_ssl_ctx_, TLS1_3_VERSION);
  SSL_CTX_set_alpn_select_cb(cached_ssl_ctx_, ALPNSelectCallback, nullptr);

  // Load cert into CTX.
  BIO* cert_bio =
      BIO_new_mem_buf(cached_cert_pem_.data(), cached_cert_pem_.size());
  X509* cert = PEM_read_bio_X509(cert_bio, nullptr, nullptr, nullptr);
  if (cert == nullptr || SSL_CTX_use_certificate(cached_ssl_ctx_, cert) <= 0) {
    LOG(ERROR) << "Proxy: Failed to load certificate into SSL_CTX.";
    X509_free(cert);
    BIO_free(cert_bio);
    SSL_CTX_free(cached_ssl_ctx_);
    cached_ssl_ctx_ = nullptr;
    return nullptr;
  }
  X509_free(cert);
  BIO_free(cert_bio);

  // Load key into CTX.
  BIO* key_bio =
      BIO_new_mem_buf(cached_key_pem_.data(), cached_key_pem_.size());
  EVP_PKEY* pkey = PEM_read_bio_PrivateKey(key_bio, nullptr, nullptr, nullptr);
  if (pkey == nullptr || SSL_CTX_use_PrivateKey(cached_ssl_ctx_, pkey) <= 0) {
    LOG(ERROR) << "Proxy: Failed to load private key into SSL_CTX.";
    EVP_PKEY_free(pkey);
    BIO_free(key_bio);
    SSL_CTX_free(cached_ssl_ctx_);
    cached_ssl_ctx_ = nullptr;
    return nullptr;
  }
  EVP_PKEY_free(pkey);
  BIO_free(key_bio);
  if (cached_ssl_ctx_) SSL_CTX_up_ref(cached_ssl_ctx_);
  return cached_ssl_ctx_;
}

}  // namespace ztab
