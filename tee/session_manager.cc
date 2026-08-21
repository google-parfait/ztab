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

#include "session_manager.h"

#include <algorithm>
#include <iomanip>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

#include "absl/log/log.h"
#include "absl/status/status.h"
#include "absl/strings/escaping.h"
#include "absl/strings/str_cat.h"
#include "absl/strings/str_replace.h"
#include "absl/time/clock.h"
#include "absl/time/time.h"
#include "google/protobuf/util/message_differencer.h"
#include "nlohmann/json.hpp"
#include "openssl/rand.h"
#include "re2/re2.h"

namespace ztab {

// =============================================================================
// ARCHITECTURE NOTE: THE TIMEOUT "TRANSMUTATION" PHILOSOPHY
// =============================================================================
// Timeouts in ZTAB are explicitly designed to decouple human/client latency
// from backend infrastructure latency. This causes timeouts to "magically
// mutate" under the hood depending on the session's state.
//
// 1. WHAT THE USER'S TIMEOUT CONTROLS (`session.timeout`):
// The user-provided timeout (default 5 minutes, up to 24 hours) is strictly
// for bounding HUMAN/CLIENT interaction delays. It dictates how long the server
// will wait for agents to join the session (OPEN state) and submit their data
// (SEALED state).
//
// 2. THE OPEN STATE DOS EXCEPTION:
// Even if a user sets a 24-hour timeout, the OPEN state specifically caps
// the timeout at 10 minutes (`kMaxOpenSeconds`). This prevents an attacker
// from exhausting the 1000-session limit by creating empty 24-hour sessions.
// Once the session advances to SEALED, the user's 24-hour timeout is honored.
//
// 3. THE CALCULATING STATE EXCEPTION (INFRASTRUCTURE LATENCY):
// Once all data is submitted, the session enters CALCULATING. The user has no
// control over the backend infrastructure (inference takes 5s on an A100 GPU
// or 9+ minutes on a CPU). It is unfair to abort a session simply because the
// backend was slow. Therefore, during CALCULATING, we completely PAUSE the
// user's timeout. Instead, we explicitly overwrite it with a 30-minute
// hard cap (`kMaxCalculatingSeconds`). This ensures legitimate slow inferences
// finish, but completely hung background threads are forcibly killed to
// protect the server.
//
// Summary of Effective Timeouts by State:
// - OPEN:         std::min(session.timeout, 10 minutes)
// - SEALED:       session.timeout
// - CALCULATING:  30 minutes (hardcoded infrastructure cap)
// =============================================================================

namespace {

constexpr int kSessionIdBytes = 16;         // 32 hex chars
constexpr int kParticipantTokenBytes = 32;  // 64 hex chars
constexpr int kDefaultTimeoutSeconds = 300;
constexpr int kMaxLlmRetries = 3;

// F8: Upper bounds on session parameters.
constexpr int kMaxParticipants = 100;
constexpr int kMaxTimeoutSeconds = 86400;  // 24 hours
constexpr int kMaxInputBytes = 65536;      // 64 KB

// Finding 2: Security limit for nested JSON parsing to
// prevent stack overflow DoS.
constexpr int kMaxJsonParseDepth = 32;

// Finding 3: Limit SEALED state waiting time.
constexpr int kMaxSealedSeconds = 600;  // 10 minutes

// Prevent unauthenticated resource exhaustion DoS.
constexpr int kMaxOpenSeconds = 600;  // 10 minutes

// Strip markdown code fences from LLM output.
// Many instruction-tuned models wrap JSON in ```json ... ``` blocks.
// This function extracts the content between the fences.
std::string StripMarkdownCodeFences(const std::string& input) {
  std::string s = input;
  std::string content;
  // Match the first ``` block, optionally with a language specifier on the
  // first line.
  // (?s) allows . to match newlines. (.*?) is lazy.
  if (RE2::PartialMatch(s, "(?s)```[a-zA-Z0-9]*\n(.*?)\n```", &content)) {
    s = content;
  } else if (RE2::PartialMatch(s, "(?s)```[a-zA-Z0-9]*\n(.*?)```", &content)) {
    // Fallback if there's no newline before the closing fence.
    s = content;
  }
  // Trim leading/trailing whitespace.
  size_t start = s.find_first_not_of(" \t\n\r");
  size_t end = s.find_last_not_of(" \t\n\r");
  if (start == std::string::npos) return s;
  return s.substr(start, end - start + 1);
}

// Parses JSON safely by enforcing a maximum recursion depth. If the depth
// exceeds kMaxJsonParseDepth, the parser callback returns false, triggering
// a parse_error and preventing stack overflow.
absl::StatusOr<nlohmann::json> ParseJsonSafely(const std::string& json_str) {
  try {
    return nlohmann::json::parse(
        json_str, [](int depth, nlohmann::json::parse_event_t,
                     nlohmann::json&) { return depth <= kMaxJsonParseDepth; });
  } catch (const nlohmann::json::parse_error& e) {
    return absl::InvalidArgumentError(
        absl::StrCat("JSON parse error (possibly exceeded max depth ",
                     kMaxJsonParseDepth, "): ", e.what()));
  }
}

// Validates a retry SubmitInput payload against the stored canonical form.
// Returns OkStatus if the payload matches, or an appropriate error:
//   - InvalidArgumentError if the payload is too large or malformed.
//   - FailedPreconditionError if the payload is valid but differs.
absl::Status ValidateRetryPayload(const std::string& retry_json,
                                  const std::string& stored_json) {
  if (static_cast<int>(retry_json.size()) > kMaxInputBytes) {
    return absl::InvalidArgumentError(
        absl::StrCat("Input payload too large (", retry_json.size(),
                     " bytes, max ", kMaxInputBytes, ")"));
  }
  auto retry_parsed = ParseJsonSafely(retry_json);
  if (!retry_parsed.ok()) {
    return absl::InvalidArgumentError(retry_parsed.status().message());
  }
  if (retry_parsed->dump() != stored_json) {
    return absl::FailedPreconditionError(
        "Input already submitted with different content");
  }
  return absl::OkStatus();
}

// Validate a single JSON value against a schema node.
// Supports: type (string, array, object, number, integer, boolean),
// pattern (regex on strings), minItems/maxItems (arrays),
// properties/required/additionalProperties (objects), items (arrays).
absl::Status ValidateNode(const nlohmann::json& value,
                          const nlohmann::json& schema) {
  if (!schema.is_object()) return absl::OkStatus();

  if (!schema.contains("type")) {
    return absl::OkStatus();  // No type constraint.
  }

  std::string type = schema["type"].get<std::string>();

  if (type == "string") {
    if (!value.is_string()) {
      return absl::InvalidArgumentError(
          absl::StrCat("Expected string, got ", value.type_name()));
    }
    if (schema.contains("pattern")) {
      std::string pattern = schema["pattern"].get<std::string>();
      std::string str_val = value.get<std::string>();
      // RE2 guarantees linear-time matching — immune to ReDoS from
      // user-supplied patterns in CreateSessionRequest.
      // PartialMatch: JSON Schema patterns are implicitly unanchored
      // (ECMA 262 semantics) unless the pattern itself uses ^ and $.
      if (!RE2::PartialMatch(str_val, pattern)) {
        // F20: Do NOT include str_val in the error — it may contain
        // private LLM output that would leak to host console logs,
        // breaking TEE confidentiality.
        return absl::InvalidArgumentError(
            absl::StrCat("String does not match pattern '", pattern, "'"));
      }
    }
  } else if (type == "array") {
    if (!value.is_array()) {
      return absl::InvalidArgumentError(
          absl::StrCat("Expected array, got ", value.type_name()));
    }
    if (schema.contains("minItems") &&
        static_cast<int>(value.size()) < schema["minItems"].get<int>()) {
      return absl::InvalidArgumentError("Array has fewer items than minItems");
    }
    if (schema.contains("maxItems") &&
        static_cast<int>(value.size()) > schema["maxItems"].get<int>()) {
      return absl::InvalidArgumentError("Array has more items than maxItems");
    }
    if (schema.contains("items")) {
      for (size_t i = 0; i < value.size(); ++i) {
        auto s = ValidateNode(value[i], schema["items"]);
        if (!s.ok()) {
          return absl::InvalidArgumentError(
              absl::StrCat("Array item ", i, ": ", s.message()));
        }
      }
    }
  } else if (type == "object") {
    if (!value.is_object()) {
      return absl::InvalidArgumentError(
          absl::StrCat("Expected object, got ", value.type_name()));
    }
    // Check required fields.
    if (schema.contains("required")) {
      for (const auto& req : schema["required"]) {
        std::string field = req.get<std::string>();
        if (!value.contains(field)) {
          return absl::InvalidArgumentError(
              absl::StrCat("Missing required field: '", field, "'"));
        }
      }
    }
    // Check additionalProperties.
    if (schema.contains("additionalProperties") &&
        schema["additionalProperties"].get<bool>() == false) {
      for (auto& [key, _] : value.items()) {
        if (!schema.contains("properties") ||
            !schema["properties"].contains(key)) {
          return absl::InvalidArgumentError(
              "Unexpected field present in object");
        }
      }
    }
    // Validate properties.
    if (schema.contains("properties")) {
      for (auto& [key, prop_schema] : schema["properties"].items()) {
        if (value.contains(key)) {
          auto s = ValidateNode(value[key], prop_schema);
          if (!s.ok()) {
            return absl::InvalidArgumentError(
                absl::StrCat("Field '", key, "': ", s.message()));
          }
        }
      }
    }
  } else if (type == "number") {
    if (!value.is_number()) {
      return absl::InvalidArgumentError(
          absl::StrCat("Expected number, got ", value.type_name()));
    }
  } else if (type == "integer") {
    if (!value.is_number_integer()) {
      return absl::InvalidArgumentError(
          absl::StrCat("Expected integer, got ", value.type_name()));
    }
  } else if (type == "boolean") {
    if (!value.is_boolean()) {
      return absl::InvalidArgumentError(
          absl::StrCat("Expected boolean, got ", value.type_name()));
    }
  }

  return absl::OkStatus();
}

}  // namespace

SessionManager::SessionManager(LlamaEngine* engine,
                               const PolicyRegistry* registry)
    : SessionManager(engine, registry, Options{}) {}

SessionManager::SessionManager(LlamaEngine* engine,
                               const PolicyRegistry* registry,
                               const Options& options)
    : engine_(engine), registry_(registry), options_(options) {}

SessionManager::~SessionManager() {
  // F13: Wait for all in-flight background threads to finish before
  // destroying members (mu_, sessions_, engine_ pointer, etc.).
  std::unique_lock<std::mutex> lock(mu_);
  while (active_async_threads_ > 0) {
    LOG(INFO) << "SessionManager shutting down, waiting for "
              << active_async_threads_ << " background thread(s)...";
    async_cv_.wait(lock);
  }
  LOG(INFO) << "SessionManager: all background threads finished, destroying.";
}

std::string SessionManager::GenerateRandomHex(int num_bytes) {
  std::vector<uint8_t> buf(num_bytes);
  if (RAND_bytes(buf.data(), num_bytes) != 1) {
    LOG(FATAL)
        << "RAND_bytes failed! Cryptographically secure RNG unavailable.";
  }
  return absl::BytesToHexString(
      absl::string_view(reinterpret_cast<const char*>(buf.data()), num_bytes));
}

bool SessionManager::IsValidUuidV4(const std::string& s) {
  static const re2::LazyRE2 kUuidV4Re = {
      "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-"
      "[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"};
  return re2::RE2::FullMatch(s, *kUuidV4Re);
}

bool SessionManager::CheckAndEnforceTimeout(Session& session) {
  if (session.state == CLOSED || session.state == ABORTED) {
    return false;
  }

  absl::Duration elapsed = absl::Now() - session.state_entered_at;
  absl::Duration effective_timeout = session.timeout;

  if (session.state == OPEN) {
    // Limit OPEN state to a maximum of 10 minutes to prevent unauthenticated
    // resource exhaustion DoS (where attackers create empty sessions to fill
    // slots).
    effective_timeout =
        std::min(effective_timeout, absl::Seconds(kMaxOpenSeconds));
  } else if (session.state == SEALED) {
    // Finding 3: Limit the time a session can sit in the SEALED state waiting
    // for inputs, preventing long-timeout sessions from locking up slots.
    effective_timeout =
        std::min(effective_timeout, absl::Seconds(kMaxSealedSeconds));
  } else if (session.state == CALCULATING) {
    // F11: LLM inference can legitimately run for minutes (~9 min on CPU).
    // Allow generous time, but add a safeguard to prevent hung threads from
    // permanently locking session slots.
    // Finding 5: Explicitly cap CALCULATING to 30 mins, bypassing overall
    // timeout.
    effective_timeout = absl::Seconds(options_.max_calculating_seconds);
  }

  if (elapsed > effective_timeout) {
    SessionError err;
    switch (session.state) {
      case OPEN:
        err = JOIN_TIMEOUT;
        break;
      case SEALED:
        err = INPUT_TIMEOUT;
        break;
      case CALCULATING:
        err = LLM_GENERATION_FAILED;
        break;
      default:
        err = SESSION_ERROR_UNSPECIFIED;
        break;
    }
    std::string old_state_name = SessionState_Name(session.state);
    session.state = ABORTED;
    session.error_code = err;
    session.error_detail =
        absl::StrCat("Session timed out in state ", old_state_name, " after ",
                     absl::FormatDuration(elapsed));
    session.state_entered_at = absl::Now();
    LOG(WARNING) << "Session " << session.id
                 << " timed out: " << session.error_detail;
    return true;
  }
  return false;
}

// DANGER: The returned pointers reference elements inside absl::flat_hash_map.
// Because flat_hash_map does NOT guarantee reference stability on rehashing,
// these pointers will become dangling (use-after-free) if any new elements
// are inserted into the maps while these pointers are being held.
//
// CONSTRAINTS:
// 1. The caller MUST hold mu_ while using these pointers.
// 2. The caller MUST NOT perform any operations that insert into sessions_
//    or session->participants while these pointers are in scope.
absl::StatusOr<
    std::pair<SessionManager::Session*, SessionManager::Participant*>>
SessionManager::Authenticate(const std::string& participant_token) {
  // One Token Per Call: look up session via reverse index.
  auto pt_it = participant_to_session_.find(participant_token);
  if (pt_it == participant_to_session_.end()) {
    // Uniform PERMISSION_DENIED — don't leak token validity.
    return absl::PermissionDeniedError("Invalid token");
  }
  const std::string& session_id = pt_it->second;

  auto session_it = sessions_.find(session_id);
  if (session_it == sessions_.end()) {
    // Session was GC'd but reverse index wasn't cleaned — shouldn't happen
    // but fail closed.
    return absl::PermissionDeniedError("Invalid token");
  }
  Session& session = session_it->second;

  auto participant_it = session.participants.find(participant_token);
  if (participant_it == session.participants.end()) {
    // Same error for invalid token — prevents enumeration.
    return absl::PermissionDeniedError("Invalid token");
  }

  return std::make_pair(&session, &participant_it->second);
}

absl::Status SessionManager::ValidateJsonSchema(
    const std::string& json_str, const std::string& schema_json) {
  auto value_or = ParseJsonSafely(json_str);
  if (!value_or.ok()) return value_or.status();

  auto schema_or = ParseJsonSafely(schema_json);
  if (!schema_or.ok()) {
    return absl::InternalError(
        absl::StrCat("Invalid schema JSON: ", schema_or.status().message()));
  }

  return ValidateNode(*value_or, *schema_or);
}

// --- RPC Implementations ---

void SessionManager::SweepTerminalSessions() {
  // Caller must hold mu_.
  absl::Time now = absl::Now();
  auto it = sessions_.begin();
  while (it != sessions_.end()) {
    Session& s = it->second;
    // F21: Actively enforce timeouts on ALL sessions during the sweep.
    // Without this, abandoned OPEN sessions never transition to ABORTED
    // (since CheckAndEnforceTimeout is normally only called during RPCs),
    // leading to permanent resource exhaustion DoS.
    CheckAndEnforceTimeout(s);

    bool should_erase = false;
    if (s.state == CLOSED || s.state == ABORTED) {
      if (s.state == ABORTED &&
          (s.error_code == JOIN_TIMEOUT || s.error_code == INPUT_TIMEOUT)) {
        // Unauthenticated or partially-authenticated sessions that stalled.
        // Delete immediately instead of waiting 1 hour to prevent resource
        // exhaustion DoS.
        should_erase = true;
      } else if ((now - s.state_entered_at) >
                 absl::Seconds(options_.terminal_retention_seconds)) {
        should_erase = true;
      }
    }

    if (should_erase) {
      LOG(INFO) << "GC: erasing terminal session " << s.id;
      // One Token Per Call: clean up reverse indexes.
      invitation_to_session_.erase(s.invitation_token);
      if (!s.creator_nonce.empty()) {
        creator_nonce_to_session_.erase(s.creator_nonce);
      }
      for (const auto& pt : s.participant_tokens) {
        participant_to_session_.erase(pt);
      }
      sessions_.erase(it++);
    } else {
      ++it;
    }
  }
}

absl::StatusOr<CreateSessionResponse> SessionManager::CreateSession(
    const CreateSessionRequest& request) {
  std::lock_guard<std::mutex> lock(mu_);

  // F7: Lazy garbage collection of terminal sessions.
  SweepTerminalSessions();

  // F7: Hard cap on total active sessions.
  if (static_cast<int>(sessions_.size()) >= options_.max_sessions) {
    return absl::ResourceExhaustedError(
        absl::StrCat("Server at session capacity (", options_.max_sessions,
                     "). Try again later."));
  }

  const auto& policy = request.policy();

  // Validate policy class.
  auto policy_def_or = registry_->GetPolicy(policy.policy_class());
  if (!policy_def_or.ok()) {
    return absl::InvalidArgumentError(policy_def_or.status().message());
  }

  // F8: Validate participant count bounds.
  if (policy.expected_participants() < 2) {
    return absl::InvalidArgumentError(
        "expected_participants must be at least 2");
  }
  if (policy.expected_participants() > kMaxParticipants) {
    return absl::InvalidArgumentError(absl::StrCat(
        "expected_participants exceeds maximum (", kMaxParticipants, ")"));
  }

  // F8: Validate timeout bounds.
  if (policy.timeout_seconds() > kMaxTimeoutSeconds) {
    return absl::InvalidArgumentError(absl::StrCat(
        "timeout_seconds exceeds maximum (", kMaxTimeoutSeconds, ")"));
  }

  // F22: Validate JSON schemas at creation time.
  // We strictly use the built-in schemas from the policy definition, ignoring
  // any client-provided overrides (F23: Prompt Injection via Schema Override).
  const std::string& input_schema = (*policy_def_or)->default_input_schema_json;
  if (!input_schema.empty()) {
    auto parsed_or = ParseJsonSafely(input_schema);
    if (!parsed_or.ok()) {
      return absl::InternalError(
          absl::StrCat("Malformed built-in input_schema_json: ",
                       parsed_or.status().message()));
    }
  }
  const std::string& output_schema =
      (*policy_def_or)->default_output_schema_json;
  if (!output_schema.empty()) {
    auto parsed_or = ParseJsonSafely(output_schema);
    if (!parsed_or.ok()) {
      return absl::InternalError(
          absl::StrCat("Malformed built-in output_schema_json: ",
                       parsed_or.status().message()));
    }
  }

  // TODO(Phase 2): Implement per-client rate limiting and/or mTLS
  // authentication to prevent unauthenticated session exhaustion DoS. Currently
  // outside scope.

  // CreateSession nonce-based idempotency.
  if (!request.client_nonce().empty()) {
    if (!IsValidUuidV4(request.client_nonce())) {
      return absl::InvalidArgumentError("client_nonce must be a valid UUIDv4");
    }
    auto nonce_it = creator_nonce_to_session_.find(request.client_nonce());
    if (nonce_it != creator_nonce_to_session_.end()) {
      // Replay: verify policy matches.
      auto sess_it = sessions_.find(nonce_it->second);
      if (sess_it != sessions_.end()) {
        SessionPolicy normalized_req_policy = request.policy();
        normalized_req_policy.set_input_schema_json(
            sess_it->second.policy.input_schema_json());
        normalized_req_policy.set_output_schema_json(
            sess_it->second.policy.output_schema_json());
        int req_timeout = request.policy().timeout_seconds() > 0
                              ? request.policy().timeout_seconds()
                              : kDefaultTimeoutSeconds;
        normalized_req_policy.set_timeout_seconds(req_timeout);
        if (!google::protobuf::util::MessageDifferencer::Equals(
                sess_it->second.policy, normalized_req_policy)) {
          return absl::InvalidArgumentError(
              "Idempotency key reused with different parameters");
        }
        // Return the original response.
        CreateSessionResponse response;
        response.set_invitation_token(sess_it->second.invitation_token);
        response.set_state(sess_it->second.state);
        // Find creator's participant_token.
        if (!sess_it->second.participant_tokens.empty()) {
          response.set_participant_token(sess_it->second.participant_tokens[0]);
        }
        return response;
      }
    }
  }

  // Create session.
  std::string session_id = GenerateRandomHex(kSessionIdBytes);
  std::string creator_token = GenerateRandomHex(kParticipantTokenBytes);
  std::string invitation_token = GenerateRandomHex(kParticipantTokenBytes);

  Session session;
  session.id = session_id;
  session.state = OPEN;
  session.policy = policy;

  // Overwrite client-provided schemas with the secure built-in ones.
  session.policy.set_input_schema_json(input_schema);
  session.policy.set_output_schema_json(output_schema);

  session.policy_def = *policy_def_or;
  session.state_entered_at = absl::Now();
  int timeout_secs = policy.timeout_seconds() > 0 ? policy.timeout_seconds()
                                                  : kDefaultTimeoutSeconds;
  session.timeout = absl::Seconds(timeout_secs);
  session.policy.set_timeout_seconds(timeout_secs);

  // One Token Per Call: store invitation token on session.
  session.invitation_token = invitation_token;

  // Register creator as first participant (already accepted — they set the
  // policy).
  Participant creator;
  creator.token = creator_token;
  creator.accepted = true;
  session.participants[creator_token] = std::move(creator);
  session.participant_tokens.push_back(creator_token);

  sessions_[session_id] = std::move(session);

  // One Token Per Call: populate reverse indexes.
  invitation_to_session_[invitation_token] = session_id;
  participant_to_session_[creator_token] = session_id;

  // CreateSession idempotency: store nonce mapping.
  if (!request.client_nonce().empty()) {
    sessions_[session_id].creator_nonce = request.client_nonce();
    creator_nonce_to_session_[request.client_nonce()] = session_id;
  }

  LOG(INFO) << "Session " << session_id
            << " created (policy=" << policy.policy_class()
            << ", expected=" << policy.expected_participants() << ")";

  CreateSessionResponse response;
  response.set_invitation_token(invitation_token);
  response.set_state(OPEN);
  response.set_participant_token(creator_token);
  return response;
}

absl::StatusOr<JoinSessionResponse> SessionManager::JoinSession(
    const JoinSessionRequest& request) {
  std::lock_guard<std::mutex> lock(mu_);

  // One Token Per Call: look up session via invitation_token reverse index.
  auto inv_it = invitation_to_session_.find(request.invitation_token());
  if (inv_it == invitation_to_session_.end()) {
    return absl::PermissionDeniedError("Invalid token");
  }
  auto session_it = sessions_.find(inv_it->second);
  if (session_it == sessions_.end()) {
    return absl::PermissionDeniedError("Invalid token");
  }
  Session& session = session_it->second;
  CheckAndEnforceTimeout(session);

  // One Token Per Call: nonce-based idempotency for JoinSession.
  // Checked before state checks so an already-joined participant can recover
  // their token even if the session advanced past OPEN.
  if (!request.client_nonce().empty()) {
    if (!IsValidUuidV4(request.client_nonce())) {
      return absl::InvalidArgumentError("client_nonce must be a valid UUIDv4");
    }
    auto nonce_it = session.nonce_to_token.find(request.client_nonce());
    if (nonce_it != session.nonce_to_token.end()) {
      // Idempotent replay — return the previously generated token.
      JoinSessionResponse response;
      response.set_state(session.state);
      *response.mutable_policy() = session.policy;
      response.set_participant_token(nonce_it->second);
      return response;
    }
  }

  if (session.state != OPEN) {
    // Return same error as "not found" to prevent session enumeration.
    return absl::PermissionDeniedError("Invalid token");
  }

  int expected = session.policy.expected_participants();
  if (static_cast<int>(session.participants.size()) >= expected) {
    // Return same error as "not found" to prevent session enumeration.
    return absl::PermissionDeniedError("Invalid token");
  }

  // Create participant.
  std::string token = GenerateRandomHex(kParticipantTokenBytes);
  Participant participant;
  participant.token = token;
  participant.join_index = session.participants.size();
  session.participants[token] = std::move(participant);
  session.participant_tokens.push_back(token);

  // One Token Per Call: populate reverse index.
  participant_to_session_[token] = session.id;

  // Store nonce mapping for idempotency.
  if (!request.client_nonce().empty()) {
    session.nonce_to_token[request.client_nonce()] = token;
  }

  LOG(INFO) << "Session " << session.id << ": participant joined ("
            << session.participants.size() << "/" << expected << ")";

  JoinSessionResponse response;
  response.set_state(session.state);
  *response.mutable_policy() = session.policy;
  response.set_participant_token(token);
  return response;
}

absl::StatusOr<AcceptPolicyResponse> SessionManager::AcceptPolicy(
    const AcceptPolicyRequest& request) {
  std::lock_guard<std::mutex> lock(mu_);

  auto auth_or = Authenticate(request.participant_token());
  if (!auth_or.ok()) return auth_or.status();
  auto [session, participant] = *auth_or;
  CheckAndEnforceTimeout(*session);

  // Idempotent: if already accepted, just return current state.
  if (participant->accepted) {
    AcceptPolicyResponse response;
    response.set_state(session->state);
    return response;
  }

  if (session->state != OPEN) {
    return absl::FailedPreconditionError(absl::StrCat(
        "Session is not OPEN (state=", SessionState_Name(session->state), ")"));
  }

  participant->accepted = true;

  // Check if all participants have joined AND accepted.
  int expected = session->policy.expected_participants();
  if (static_cast<int>(session->participants.size()) == expected) {
    bool all_accepted = true;
    for (const auto& [_, p] : session->participants) {
      if (!p.accepted) {
        all_accepted = false;
        break;
      }
    }
    if (all_accepted) {
      session->state = SEALED;
      session->state_entered_at = absl::Now();
      LOG(INFO) << "Session " << session->id
                << ": all participants accepted, state -> SEALED";
    }
  }

  AcceptPolicyResponse response;
  response.set_state(session->state);
  return response;
}

absl::StatusOr<SubmitInputResponse> SessionManager::SubmitInput(
    const SubmitInputRequest& request) {
  // Two-phase approach: validate and store input under lock, then release
  // the lock before calling the LLM (which can take minutes on CPU).
  // This allows other sessions/RPCs to proceed during LLM inference.

  std::string session_id;
  bool should_process = false;

  {
    std::lock_guard<std::mutex> lock(mu_);

    auto auth_or = Authenticate(request.participant_token());
    if (!auth_or.ok()) return auth_or.status();
    auto [session, participant] = *auth_or;
    CheckAndEnforceTimeout(*session);
    session_id = session->id;

    if (session->state != SEALED) {
      // Idempotent: if already in CALCULATING/CLOSED/ABORTED, return state.
      if (participant->input_submitted) {
        // Validate retry payload against stored canonical form.
        auto retry_status =
            ValidateRetryPayload(request.input_json(), participant->input_json);
        if (!retry_status.ok()) return retry_status;
        SubmitInputResponse response;
        response.set_state(session->state);
        response.set_remaining_inputs(0);
        return response;
      }
      return absl::FailedPreconditionError(absl::StrCat(
          "Session is not SEALED (state=", SessionState_Name(session->state),
          ")"));
    }

    if (participant->input_submitted) {
      // Idempotent: already submitted in SEALED state. Verify content.
      auto retry_status =
          ValidateRetryPayload(request.input_json(), participant->input_json);
      if (!retry_status.ok()) return retry_status;
      int submitted = 0;
      for (const auto& [_, p] : session->participants) {
        if (p.input_submitted) ++submitted;
      }
      SubmitInputResponse response;
      response.set_state(session->state);
      response.set_remaining_inputs(session->policy.expected_participants() -
                                    submitted);
      return response;
    }

    // F8: Reject oversized input before parsing.
    if (static_cast<int>(request.input_json().size()) > kMaxInputBytes) {
      return absl::InvalidArgumentError(
          absl::StrCat("Input payload too large (", request.input_json().size(),
                       " bytes, max ", kMaxInputBytes, ")"));
    }

    // Normalize input by parsing and dumping to strip duplicate keys, comments,
    // etc. This prevents parser differentials (e.g., duplicate keys used for
    // prompt injection).
    auto parsed_or = ParseJsonSafely(request.input_json());
    if (!parsed_or.ok()) return parsed_or.status();
    nlohmann::json parsed_input = std::move(*parsed_or);

    // Validate input against schema.
    std::string input_schema = session->policy.input_schema_json();
    if (input_schema.empty() && session->policy_def != nullptr) {
      input_schema = session->policy_def->default_input_schema_json;
    }
    if (!input_schema.empty()) {
      auto validate_status =
          ValidateJsonSchema(parsed_input.dump(), input_schema);
      if (!validate_status.ok()) {
        return absl::InvalidArgumentError(absl::StrCat(
            "Input schema violation: ", validate_status.message()));
      }
    }

    participant->input_json = parsed_input.dump();
    participant->input_submitted = true;

    // Count remaining inputs.
    int submitted = 0;
    for (const auto& [_, p] : session->participants) {
      if (p.input_submitted) ++submitted;
    }
    int remaining = session->policy.expected_participants() - submitted;

    LOG(INFO) << "Session " << session->id << ": input submitted (" << submitted
              << "/" << session->policy.expected_participants() << ")";

    if (remaining == 0) {
      // All inputs received — transition to CALCULATING.
      session->state = CALCULATING;
      session->state_entered_at = absl::Now();
      LOG(INFO) << "Session " << session->id
                << ": all inputs received, state -> CALCULATING";
      should_process = true;
      ++active_async_threads_;  // Track thread inside main lock to prevent
                                // use-after-free
    } else {
      // Not all inputs yet — return immediately.
      SubmitInputResponse response;
      response.set_state(session->state);
      response.set_remaining_inputs(remaining);
      return response;
    }
  }
  // Lock released here.

  // F10: Dispatch LLM processing to a background thread so the gRPC
  // handler thread is freed immediately. The client polls via GetResult.
  if (should_process) {
    try {
      std::thread([this, session_id]() {
        ProcessSessionAsync(session_id);
      }).detach();
    } catch (const std::system_error& e) {
      // Finding 4: Catch std::system_error (e.g. EAGAIN) when spawning thread.
      // Revert the thread counter increment inside lock and return an error.
      std::lock_guard<std::mutex> lock(mu_);
      --active_async_threads_;
      async_cv_.notify_all();

      auto it = sessions_.find(session_id);
      if (it != sessions_.end()) {
        it->second.state = ABORTED;
        it->second.error_code = SESSION_ERROR_UNSPECIFIED;
        it->second.error_detail =
            absl::StrCat("Failed to spawn inference thread: ", e.what());
      }
      return absl::InternalError(
          "Failed to spawn LLM background thread: resource exhausted");
    }
  }

  // Return immediately with CALCULATING state.
  SubmitInputResponse response;
  response.set_state(CALCULATING);
  response.set_remaining_inputs(0);
  return response;
}

void SessionManager::ProcessSessionAsync(const std::string& session_id) {
  // F13: RAII guard to decrement active_async_threads_ on ALL exit paths
  // and notify the destructor's condition variable.
  struct ThreadGuard {
    SessionManager* mgr;
    ~ThreadGuard() {
      std::lock_guard<std::mutex> lock(mgr->mu_);
      --mgr->active_async_threads_;
      mgr->async_cv_.notify_all();
    }
  } guard{this};

  // Step 1: Build prompt (needs session data — lock briefly).
  std::string prompt;
  std::string output_schema;
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = sessions_.find(session_id);
    if (it == sessions_.end()) {
      LOG(ERROR) << "Session " << session_id
                 << " disappeared before async processing";
      return;
    }
    Session& session = it->second;
    if (session.state != CALCULATING) {
      LOG(WARNING) << "Session " << session_id
                   << " no longer CALCULATING, aborting async processing";
      return;
    }

    // Build aggregated prompt with deterministic participant ordering.
    // absl::flat_hash_map iteration is non-deterministic, so we sort
    // by join_index to ensure chronological LLM prompt construction.
    std::vector<std::string> sorted_tokens;
    sorted_tokens.reserve(session.participants.size());
    for (const auto& [token, _] : session.participants) {
      sorted_tokens.push_back(token);
    }
    std::sort(sorted_tokens.begin(), sorted_tokens.end(),
              [&session](const std::string& a, const std::string& b) {
                return session.participants.at(a).join_index <
                       session.participants.at(b).join_index;
              });

    // Generate a random suffix for the delimiters to prevent prompt injection
    // where a participant might try to include the end marker in their input.
    std::string delim_suffix = GenerateRandomHex(8);

    std::ostringstream inputs_block;
    int participant_num = 1;
    for (const auto& token : sorted_tokens) {
      const auto& p = session.participants.at(token);
      inputs_block << "<<<PARTICIPANT_" << participant_num << "_INPUT_BEGIN_"
                   << delim_suffix << ">>>\n"
                   << p.input_json << "\n"
                   << "<<<PARTICIPANT_" << participant_num << "_INPUT_END_"
                   << delim_suffix << ">>>\n\n";
      ++participant_num;
    }
    prompt = session.policy_def->prompt_template;
    prompt = absl::StrReplaceAll(
        prompt,
        {
            {"{num_participants}", std::to_string(session.participants.size())},
            {"{inputs}", inputs_block.str()},
        });

    output_schema = session.policy.output_schema_json();
    if (output_schema.empty()) {
      output_schema = session.policy_def->default_output_schema_json;
    }

    LOG(INFO) << "Session " << session.id << ": aggregated prompt ("
              << prompt.size() << " bytes)";
  }
  // Lock released — other RPCs can proceed now.

  // Step 2: Call LLM OUTSIDE the lock (this is the slow part).
  std::string result_json;
  absl::Status process_status = absl::OkStatus();
  for (int attempt = 0; attempt < kMaxLlmRetries; ++attempt) {
    if (engine_ == nullptr) {
      process_status = absl::InternalError("No LLM engine available");
      break;
    }

    auto result = engine_->Generate(prompt, 4096);
    if (!result.ok()) {
      LOG(WARNING) << "Session " << session_id
                   << ": LLM generation failed (attempt " << attempt + 1
                   << "): " << result.status().message();
      continue;
    }

    LOG(INFO) << "Session " << session_id << ": LLM raw output (attempt "
              << attempt + 1 << "): " << result->size() << " bytes";

    std::string output = StripMarkdownCodeFences(*result);

    if (!output_schema.empty()) {
      auto validate_status = ValidateJsonSchema(output, output_schema);
      if (!validate_status.ok()) {
        LOG(WARNING) << "Session " << session_id
                     << ": output schema violation (attempt " << attempt + 1
                     << "): " << validate_status.message();
        continue;
      }
    }

    // Success.
    result_json = output;
    break;
  }

  if (result_json.empty() && process_status.ok()) {
    process_status = absl::InternalError(
        absl::StrCat("LLM failed to produce valid output after ",
                     kMaxLlmRetries, " attempts"));
  }

  // Step 3: Re-lock to write results.
  // F11: Guard against zombie resurrection — only write if still CALCULATING.
  {
    std::lock_guard<std::mutex> lock(mu_);
    auto it = sessions_.find(session_id);
    if (it == sessions_.end()) {
      LOG(WARNING) << "Session " << session_id
                   << " was GC'd during LLM processing";
      return;
    }
    Session& session = it->second;

    // F11: If the session was ABORTED (e.g. by timeout or admin) while
    // we were running the LLM, respect that decision — don't resurrect.
    if (session.state != CALCULATING) {
      LOG(WARNING) << "Session " << session.id << ": state changed to "
                   << SessionState_Name(session.state)
                   << " during LLM processing, discarding result";
      return;
    }

    if (process_status.ok()) {
      session.result_json = result_json;
      session.state = CLOSED;
      session.state_entered_at = absl::Now();
      LOG(INFO) << "Session " << session.id << ": state -> CLOSED";
    } else {
      session.state = ABORTED;
      session.error_code = LLM_GENERATION_FAILED;
      session.error_detail = std::string(process_status.message());
      session.state_entered_at = absl::Now();
      LOG(ERROR) << "Session " << session.id
                 << " processing failed: " << process_status.message();
    }
  }
}

absl::StatusOr<GetResultResponse> SessionManager::GetResult(
    const GetResultRequest& request) {
  std::lock_guard<std::mutex> lock(mu_);

  auto auth_or = Authenticate(request.participant_token());
  if (!auth_or.ok()) return auth_or.status();
  auto [session, _] = *auth_or;
  CheckAndEnforceTimeout(*session);

  GetResultResponse response;
  response.set_state(session->state);

  if (session->state == CLOSED) {
    response.set_result_json(session->result_json);
  } else if (session->state == ABORTED) {
    response.set_error_code(session->error_code);
    response.set_error_detail(session->error_detail);
  }

  return response;
}

absl::StatusOr<GetSessionStatusResponse> SessionManager::GetSessionStatus(
    const GetSessionStatusRequest& request) {
  std::lock_guard<std::mutex> lock(mu_);

  auto auth_or = Authenticate(request.participant_token());
  if (!auth_or.ok()) return auth_or.status();
  auto [session, _] = *auth_or;
  CheckAndEnforceTimeout(*session);

  int joined = session->participants.size();
  int accepted = 0;
  int inputs = 0;
  for (const auto& [_, p] : session->participants) {
    if (p.accepted) ++accepted;
    if (p.input_submitted) ++inputs;
  }

  GetSessionStatusResponse response;
  response.set_state(session->state);
  response.set_participants_joined(joined);
  response.set_participants_accepted(accepted);
  response.set_inputs_received(inputs);
  return response;
}

}  // namespace ztab
