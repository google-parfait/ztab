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

#ifndef ZTAB_SERVER_SESSION_MANAGER_H_
#define ZTAB_SERVER_SESSION_MANAGER_H_

#include <condition_variable>
#include <memory>
#include <string>
#include <thread>
#include <vector>

#include "absl/container/flat_hash_map.h"
#include "absl/status/status.h"
#include "absl/status/statusor.h"
#include "absl/time/time.h"
#include "llama_engine.h"
#include "policy_registry.h"
#include "session_manager.grpc.pb.h"

namespace ztab {

// Manages the lifecycle of multi-agent sessions within the TEE.
//
// Thread-safety: All public methods are thread-safe. A single mutex protects
// the session map. When all inputs are received, LLM inference is dispatched
// to a background thread to avoid blocking the gRPC thread pool.
// The background thread acquires the lock briefly to read session data,
// runs inference without the lock, then re-acquires it to write results.
// The destructor waits for all in-flight background threads to complete.
//
// Timeout enforcement: Lazy — checked on every RPC access. No background
// reaper thread. Sessions that exceed their per-state timeout are transitioned
// to ABORTED when next accessed. The CALCULATING state is exempt from
// timeouts since LLM inference has its own retry/failure semantics.
class SessionManager {
 public:
  // Does not take ownership of engine or registry. Both must outlive this.
  SessionManager(LlamaEngine* engine, const PolicyRegistry* registry);

  // F13: Destructor waits for any in-flight background threads to complete
  // before allowing member destruction, preventing dangling-pointer UB.
  ~SessionManager();

  // --- Session RPCs ---

  absl::StatusOr<CreateSessionResponse> CreateSession(
      const CreateSessionRequest& request);

  absl::StatusOr<JoinSessionResponse> JoinSession(
      const JoinSessionRequest& request);

  absl::StatusOr<AcceptPolicyResponse> AcceptPolicy(
      const AcceptPolicyRequest& request);

  absl::StatusOr<SubmitInputResponse> SubmitInput(
      const SubmitInputRequest& request);

  absl::StatusOr<GetResultResponse> GetResult(const GetResultRequest& request);

  absl::StatusOr<GetSessionStatusResponse> GetSessionStatus(
      const GetSessionStatusRequest& request);

 private:
  struct Participant {
    std::string token;
    bool accepted = false;
    std::string input_json;  // Empty until submitted.
    bool input_submitted = false;
    int join_index = 0;
  };

  struct Session {
    std::string id;
    SessionState state = OPEN;
    SessionPolicy policy;
    const PolicyDefinition* policy_def = nullptr;
    absl::flat_hash_map<std::string, Participant>
        participants;  // token -> participant
    std::string result_json;
    SessionError error_code = SESSION_ERROR_UNSPECIFIED;
    std::string error_detail;
    absl::Time state_entered_at;
    absl::Duration timeout;
    // One Token Per Call: invitation token for this session.
    std::string invitation_token;
    // All participant tokens for O(1) GC cleanup.
    std::vector<std::string> participant_tokens;
    // JoinSession idempotency: client_nonce -> participant_token.
    absl::flat_hash_map<std::string, std::string> nonce_to_token;
    // CreateSession idempotency: the nonce used to create
    // this session.
    std::string creator_nonce;
  };

  // Generate a cryptographically random hex string of the given byte length.
  static std::string GenerateRandomHex(int num_bytes);

  // Validate that a string is a valid UUIDv4 (RFC 4122).
  static bool IsValidUuidV4(const std::string& s);

  // Check if the session has timed out in its current state. If so,
  // transitions to ABORTED and returns true.
  bool CheckAndEnforceTimeout(Session& session);

  // Authenticate: looks up session via participant_token reverse index.
  // Returns PERMISSION_DENIED if token is invalid.
  absl::StatusOr<std::pair<Session*, Participant*>> Authenticate(
      const std::string& participant_token);

  // Lazy garbage collection: erase terminal sessions (CLOSED/ABORTED) that
  // have been in that state longer than kTerminalRetentionSeconds. Called
  // at the start of CreateSession to bound memory usage.
  void SweepTerminalSessions();

  // Asynchronous LLM processing: builds the prompt, calls the LLM engine,
  // and writes the result back to the session. Runs on a detached thread
  // to avoid blocking the gRPC thread pool (F10).
  void ProcessSessionAsync(const std::string& session_id);

  // Validate a JSON string against a JSON Schema (supporting type, pattern,
  // properties, required, minItems, maxItems, additionalProperties).
  absl::Status ValidateJsonSchema(const std::string& json_str,
                                  const std::string& schema_json);

  LlamaEngine* engine_;             // Not owned.
  const PolicyRegistry* registry_;  // Not owned.
  mutable std::mutex mu_;
  absl::flat_hash_map<std::string, Session> sessions_;

  // One Token Per Call: reverse indexes for token -> session lookup.
  absl::flat_hash_map<std::string, std::string>
      invitation_to_session_;  // invitation_token -> session_id
  absl::flat_hash_map<std::string, std::string>
      participant_to_session_;  // participant_token -> session_id

  // CreateSession idempotency: creator_nonce -> session_id.
  absl::flat_hash_map<std::string, std::string> creator_nonce_to_session_;

  // F13: Track active background threads to enable safe shutdown.
  std::condition_variable async_cv_;  // Notified when a thread finishes.
  int active_async_threads_ = 0;      // Guarded by mu_.
};

}  // namespace ztab

#endif  // ZTAB_SERVER_SESSION_MANAGER_H_
