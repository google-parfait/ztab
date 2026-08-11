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

#include <openssl/crypto.h>

#include <csignal>
#include <iostream>
#include <memory>
#include <string>
#include <utility>

#include "absl/flags/flag.h"
#include "absl/flags/parse.h"
#include "absl/log/globals.h"
#include "absl/log/initialize.h"
#include "absl/log/log.h"
#include "absl/status/statusor.h"
#include "absl/strings/str_cat.h"
#include "absl/time/clock.h"
#include "absl/time/time.h"
#include "attestation_token_provider.h"
#include "gcp_attestation_token_provider.h"
#include "grpcpp/grpcpp.h"
#include "grpcpp/security/server_credentials.h"
#include "llama_engine.h"
#include "mock_attestation_token_provider.h"
#include "policy_registry.h"
#include "session_manager.grpc.pb.h"
#include "session_manager.h"
#include "tls_proxy.h"

ABSL_FLAG(int32_t, port, 8000, "The port on which the server should listen.");

ABSL_FLAG(std::string, attestation_provider, "mock",
          "Which attestation provider to use: 'mock' for local development "
          "(unsigned JWT), or 'ita' for real Intel Trust Authority attestation "
          "via the GCP Confidential Space agent.");

ABSL_FLAG(std::string, model_path, "",
          "Path to a GGUF model file. If empty, the server runs without LLM "
          "support and Echo just echoes the input.");

ABSL_FLAG(int32_t, gpu_layers, 0,
          "Number of model layers to offload to GPU. Use 999 for full "
          "offload (e.g., on H100). Default: 0 (CPU only).");

ABSL_FLAG(int32_t, local_port, 8001,
          "Port for the local insecure gRPC server on loopback (127.0.0.1). "
          "This port is not accessible from outside the container.");

ABSL_FLAG(std::string, policy_dir, "",
          "Directory containing *.json policy files to load at startup. "
          "If empty, no policies are loaded and session creation will fail. "
          "Example: --policy_dir=examples/calendar/");

ABSL_FLAG(std::string, creator_token, "",
          "Pre-shared token for gating CreateSession. If set, clients must "
          "include this token in the x-ztab-creator-token gRPC metadata "
          "header. If empty, CreateSession is ungated (pilot mode). "
          "For production: inject post-boot via KMS (see §6.2).");

namespace ztab {
namespace {

// Helper: convert absl::Status to grpc::Status.
grpc::Status AbslToGrpc(const absl::Status& status) {
  if (status.ok()) return grpc::Status::OK;
  grpc::StatusCode code;
  switch (status.code()) {
    case absl::StatusCode::kPermissionDenied:
      code = grpc::StatusCode::PERMISSION_DENIED;
      break;
    case absl::StatusCode::kFailedPrecondition:
      code = grpc::StatusCode::FAILED_PRECONDITION;
      break;
    case absl::StatusCode::kInvalidArgument:
      code = grpc::StatusCode::INVALID_ARGUMENT;
      break;
    case absl::StatusCode::kNotFound:
      code = grpc::StatusCode::NOT_FOUND;
      break;
    case absl::StatusCode::kResourceExhausted:
      code = grpc::StatusCode::RESOURCE_EXHAUSTED;
      break;
    case absl::StatusCode::kInternal:
      code = grpc::StatusCode::INTERNAL;
      break;
    default:
      code = grpc::StatusCode::UNKNOWN;
      break;
  }
  return grpc::Status(code, std::string(status.message()));
}

// gRPC service implementation that delegates session RPCs to SessionManager
// and keeps the original Echo RPC for backward compatibility / health checks.
class AgentBrokerServiceImpl final : public AgentBrokerService::Service {
 public:
  AgentBrokerServiceImpl(LlamaEngine* engine, SessionManager* session_mgr)
      : engine_(engine), session_mgr_(session_mgr) {}

  grpc::Status Echo(grpc::ServerContext* context, const EchoRequest* request,
                    EchoResponse* response) override {
    LOG(INFO) << "Received Echo request: " << request->message();

    if (engine_ != nullptr) {
      // LLM mode: treat the message as a prompt.
      LOG(INFO) << "Running LLM inference...";
      absl::Time infer_start = absl::Now();
      auto result = engine_->Generate(request->message());
      absl::Duration infer_elapsed = absl::Now() - infer_start;
      if (!result.ok()) {
        LOG(ERROR) << "LLM generation failed: " << result.status().ToString();
        return grpc::Status(grpc::StatusCode::INTERNAL,
                            result.status().ToString());
      }
      response->set_message(*result);
      LOG(INFO) << "LLM generation complete (" << result->size()
                << " bytes) in " << absl::FormatDuration(infer_elapsed)
                << " (wall time).";
    } else {
      // Simple echo mode (no model loaded).
      response->set_message(absl::StrCat("Echo: ", request->message()));
    }

    return grpc::Status::OK;
  }

  grpc::Status CreateSession(grpc::ServerContext* context,
                             const CreateSessionRequest* request,
                             CreateSessionResponse* response) override {
    // One Token Per Call: gate CreateSession with creator_token.
    // TODO: b/438809953 — Replace this inline check with a pluggable
    // AdmissionInterceptor class hierarchy (BearerTokenInterceptor /
    // AllowAllInterceptor) as specified in design doc §3.
    std::string required_token = absl::GetFlag(FLAGS_creator_token);
    if (required_token.empty()) {
      const char* env_token = std::getenv("CREATOR_TOKEN");
      if (env_token != nullptr) {
        required_token = env_token;
      }
    }
    if (!required_token.empty()) {
      auto metadata = context->client_metadata();
      auto it = metadata.find("x-ztab-creator-token");
      std::string provided =
          (it != metadata.end())
              ? std::string(it->second.data(), it->second.size())
              : "";
      if (it == metadata.end() || provided.size() != required_token.size() ||
          CRYPTO_memcmp(provided.data(), required_token.data(),
                        required_token.size()) != 0) {
        return grpc::Status(grpc::StatusCode::PERMISSION_DENIED,
                            "Invalid token");
      }
    }
    auto result = session_mgr_->CreateSession(*request);
    if (!result.ok()) return AbslToGrpc(result.status());
    *response = *result;
    return grpc::Status::OK;
  }

  grpc::Status JoinSession(grpc::ServerContext* context,
                           const JoinSessionRequest* request,
                           JoinSessionResponse* response) override {
    auto result = session_mgr_->JoinSession(*request);
    if (!result.ok()) return AbslToGrpc(result.status());
    *response = *result;
    return grpc::Status::OK;
  }

  grpc::Status AcceptPolicy(grpc::ServerContext* context,
                            const AcceptPolicyRequest* request,
                            AcceptPolicyResponse* response) override {
    auto result = session_mgr_->AcceptPolicy(*request);
    if (!result.ok()) return AbslToGrpc(result.status());
    *response = *result;
    return grpc::Status::OK;
  }

  grpc::Status SubmitInput(grpc::ServerContext* context,
                           const SubmitInputRequest* request,
                           SubmitInputResponse* response) override {
    auto result = session_mgr_->SubmitInput(*request);
    if (!result.ok()) return AbslToGrpc(result.status());
    *response = *result;
    return grpc::Status::OK;
  }

  grpc::Status GetResult(grpc::ServerContext* context,
                         const GetResultRequest* request,
                         GetResultResponse* response) override {
    auto result = session_mgr_->GetResult(*request);
    if (!result.ok()) return AbslToGrpc(result.status());
    *response = *result;
    return grpc::Status::OK;
  }

  grpc::Status GetSessionStatus(grpc::ServerContext* context,
                                const GetSessionStatusRequest* request,
                                GetSessionStatusResponse* response) override {
    auto result = session_mgr_->GetSessionStatus(*request);
    if (!result.ok()) return AbslToGrpc(result.status());
    *response = *result;
    return grpc::Status::OK;
  }

 private:
  LlamaEngine* engine_;          // Not owned.
  SessionManager* session_mgr_;  // Not owned.
};

volatile sig_atomic_t g_shutdown_requested = 0;

void RunServer() {
  absl::InitializeLog();
  absl::SetStderrThreshold(absl::LogSeverityAtLeast::kInfo);

  // --- LLM Engine (optional) ---
  std::unique_ptr<LlamaEngine> engine;
  std::string model_path = absl::GetFlag(FLAGS_model_path);
  if (!model_path.empty()) {
    int gpu_layers = absl::GetFlag(FLAGS_gpu_layers);
    LOG(INFO) << "Loading LLM model from " << model_path
              << " (gpu_layers=" << gpu_layers << ")...";
    absl::Time load_start = absl::Now();
    auto engine_or = CreateLlamaEngine(model_path, gpu_layers);
    if (!engine_or.ok()) {
      LOG(ERROR) << "Failed to load LLM: " << engine_or.status().ToString();
      return;
    }
    engine = std::move(*engine_or);
    absl::Duration load_elapsed = absl::Now() - load_start;
    LOG(INFO) << "LLM model loaded successfully in "
              << absl::FormatDuration(load_elapsed) << ".";
  } else {
    LOG(INFO) << "No --model_path specified; running in echo-only mode.";
  }

  // --- Policy Registry ---
  PolicyRegistry policy_registry;
  std::string policy_dir = absl::GetFlag(FLAGS_policy_dir);
  if (!policy_dir.empty()) {
    auto status = policy_registry.LoadFromDirectory(policy_dir);
    if (!status.ok()) {
      LOG(FATAL) << "Failed to load policies from '" << policy_dir
                 << "': " << status.ToString();
    }
    LOG(INFO) << "Loaded " << policy_registry.size() << " policies from "
              << policy_dir;
  } else {
    LOG(WARNING) << "No --policy_dir specified. No policies loaded. "
                 << "Session creation will fail.";
  }

  // --- Session Manager ---
  SessionManager session_mgr(engine.get(), &policy_registry);
  LOG(INFO) << "Session manager initialized.";

  // --- Attestation Provider ---
  std::string provider_flag = absl::GetFlag(FLAGS_attestation_provider);
  std::unique_ptr<AttestationTokenProvider> attestation_provider;

  if (provider_flag == "mock") {
    LOG(INFO) << "Using mock attestation provider (local development).";
    attestation_provider = CreateMockAttestationTokenProvider();
  } else if (provider_flag == "ita") {
    LOG(INFO) << "Using ITA attestation provider (GCP Confidential Space).";
    attestation_provider = CreateGcpAttestationTokenProvider();
  } else {
    LOG(ERROR) << "Invalid --attestation_provider value: '" << provider_flag
               << "'. Use 'mock' or 'ita'.";
    return;
  }

  // --- Start Local Insecure gRPC Server ---
  int local_port = absl::GetFlag(FLAGS_local_port);
  std::string local_address = absl::StrCat("127.0.0.1:", local_port);
  AgentBrokerServiceImpl service(engine.get(), &session_mgr);

  grpc::ServerBuilder builder;
  // Listen only on loopback for security, since proxy handles TLS.
  builder.AddListeningPort(local_address, grpc::InsecureServerCredentials());
  builder.RegisterService(&service);
  std::unique_ptr<grpc::Server> server(builder.BuildAndStart());
  LOG(INFO) << "Local gRPC Server listening on " << local_address;

  // --- Start Public TLS Proxy ---
  // Architecture: Standard TLS-terminating reverse proxy pattern (like
  // nginx/envoy). gRPC runs insecure on loopback (inaccessible externally),
  // while TlsProxy terminates TLS on the public port using ephemeral RA-TLS
  // certificates. This decouples attestation token lifecycle from gRPC's
  // internal SSL context caching (which cannot refresh certs per-handshake).
  int public_port = absl::GetFlag(FLAGS_port);
  TlsProxy proxy(public_port, local_port, attestation_provider.get());
  absl::Status proxy_status = proxy.Start();
  if (!proxy_status.ok()) {
    LOG(ERROR) << "Failed to start TLS Proxy: " << proxy_status.ToString();
    return;
  }

  // Install signal handlers so that proxy.Stop() drain logic executes on
  // container shutdown (SIGTERM from Docker/Borg, SIGINT from Ctrl-C).
  auto shutdown_handler = [](int signum) { g_shutdown_requested = 1; };
  std::signal(SIGTERM, shutdown_handler);
  std::signal(SIGINT, shutdown_handler);

  // Poll until shutdown is requested
  while (!g_shutdown_requested) {
    std::this_thread::sleep_for(std::chrono::milliseconds(100));
  }

  LOG(INFO) << "Shutdown signal received, shutting down gRPC server...";
  server->Shutdown();
  proxy.Stop();
}

}  // namespace
}  // namespace ztab

int main(int argc, char** argv) {
  absl::ParseCommandLine(argc, argv);
  ztab::RunServer();
  return 0;
}
