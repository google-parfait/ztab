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

#include <chrono>
#include <string>
#include <thread>
#include <vector>

#include "absl/status/status.h"
#include "gmock/gmock.h"
#include "gtest/gtest.h"
#include "llama_engine.h"
#include "policy_registry.h"
#include "session_manager.grpc.pb.h"

namespace ztab {
namespace {

using ::testing::Eq;
using ::testing::Ne;

const char kTestInputSchema[] = R"({
  "type": "object",
  "properties": {
    "slots": {
      "type": "array",
      "items": {"type": "string"}
    }
  },
  "required": ["slots"],
  "additionalProperties": false
})";

const char kTestOutputSchema[] = R"({
  "type": "object",
  "properties": {
    "overlap": {"type": "string", "pattern": "^[0-9TZ:-]+$"}
  }
})";

class SessionManagerTest : public ::testing::Test {
 protected:
  void SetUp() override {
    PolicyDefinition def;
    def.prompt_template = "Overlap for {num_participants}: {inputs}";
    def.default_input_schema_json = kTestInputSchema;
    def.default_output_schema_json = kTestOutputSchema;
    registry_.RegisterPolicy("ScheduleOverlap", std::move(def));

    session_mgr_ = std::make_unique<SessionManager>(
        /*engine=*/nullptr, &registry_);
  }

  SessionPolicy MakeDefaultPolicy(int expected_participants = 2) {
    SessionPolicy policy;
    policy.set_policy_class("ScheduleOverlap");
    policy.set_expected_participants(expected_participants);
    policy.set_timeout_seconds(300);
    return policy;
  }

  PolicyRegistry registry_;
  std::unique_ptr<SessionManager> session_mgr_;
};

// =============================================================================
// Test Suite A: UUIDv4 Validation
// =============================================================================

TEST_F(SessionManagerTest, UuidValidation_ValidLowercase) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  req.set_client_nonce("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11");
  auto resp_or = session_mgr_->CreateSession(req);
  EXPECT_TRUE(resp_or.ok());
}

TEST_F(SessionManagerTest, UuidValidation_ValidUppercase) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  req.set_client_nonce("A0EEBC99-9C0B-4EF8-BB6D-6BB9BD380A11");
  auto resp_or = session_mgr_->CreateSession(req);
  EXPECT_TRUE(resp_or.ok());
}

TEST_F(SessionManagerTest, UuidValidation_InvalidVersion) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  // Version nibble is '1' instead of '4'
  req.set_client_nonce("a0eebc99-9c0b-1ef8-bb6d-6bb9bd380a11");
  auto resp_or = session_mgr_->CreateSession(req);
  EXPECT_FALSE(resp_or.ok());
  EXPECT_EQ(resp_or.status().code(), absl::StatusCode::kInvalidArgument);
}

TEST_F(SessionManagerTest, UuidValidation_InvalidVariant) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  // Variant nibble is '0' instead of [89ab]
  req.set_client_nonce("a0eebc99-9c0b-4ef8-0b6d-6bb9bd380a11");
  auto resp_or = session_mgr_->CreateSession(req);
  EXPECT_FALSE(resp_or.ok());
  EXPECT_EQ(resp_or.status().code(), absl::StatusCode::kInvalidArgument);
}

TEST_F(SessionManagerTest, UuidValidation_InvalidFormat) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  req.set_client_nonce("not-a-valid-uuid");
  auto resp_or = session_mgr_->CreateSession(req);
  EXPECT_FALSE(resp_or.ok());
  EXPECT_EQ(resp_or.status().code(), absl::StatusCode::kInvalidArgument);
}

// =============================================================================
// Test Suite B: CreateSession Idempotency
// =============================================================================

TEST_F(SessionManagerTest, CreateSession_NoNonce) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  auto resp_or = session_mgr_->CreateSession(req);
  ASSERT_TRUE(resp_or.ok());
  EXPECT_FALSE(resp_or->invitation_token().empty());
  EXPECT_FALSE(resp_or->participant_token().empty());
  EXPECT_EQ(resp_or->state(), OPEN);
}

TEST_F(SessionManagerTest, CreateSession_IdempotentReplayReturnsSameTokens) {
  const std::string kNonce = "f47ac10b-58cc-4372-a567-0e02b2c3d479";
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  req.set_client_nonce(kNonce);

  auto resp1 = session_mgr_->CreateSession(req);
  ASSERT_TRUE(resp1.ok());

  // Exact duplicate replay
  auto resp2 = session_mgr_->CreateSession(req);
  ASSERT_TRUE(resp2.ok());

  EXPECT_EQ(resp1->invitation_token(), resp2->invitation_token());
  EXPECT_EQ(resp1->participant_token(), resp2->participant_token());
  EXPECT_EQ(resp1->state(), resp2->state());
}

TEST_F(SessionManagerTest, CreateSession_ReplayDefaultNormalization) {
  const std::string kNonce = "c9a646d3-9c61-4cd7-9f59-e1b630d7d80f";
  // Request 1: timeout_seconds is 0 (server defaults to 300)
  CreateSessionRequest req1;
  req1.mutable_policy()->set_policy_class("ScheduleOverlap");
  req1.mutable_policy()->set_expected_participants(2);
  req1.set_client_nonce(kNonce);

  auto resp1 = session_mgr_->CreateSession(req1);
  ASSERT_TRUE(resp1.ok());

  // Request 2: exact retransmit of unnormalized request
  CreateSessionRequest req2;
  req2.mutable_policy()->set_policy_class("ScheduleOverlap");
  req2.mutable_policy()->set_expected_participants(2);
  req2.set_client_nonce(kNonce);

  auto resp2 = session_mgr_->CreateSession(req2);
  ASSERT_TRUE(resp2.ok());
  EXPECT_EQ(resp1->invitation_token(), resp2->invitation_token());
  EXPECT_EQ(resp1->participant_token(), resp2->participant_token());
}

TEST_F(SessionManagerTest, CreateSession_ReplayPolicyMismatchFails) {
  const std::string kNonce = "e2d83b38-6b22-4a7b-83c9-04d9c7921a2b";
  CreateSessionRequest req1;
  *req1.mutable_policy() = MakeDefaultPolicy(2);
  req1.set_client_nonce(kNonce);

  auto resp1 = session_mgr_->CreateSession(req1);
  ASSERT_TRUE(resp1.ok());

  // Request 2 reuses nonce with different participant count
  CreateSessionRequest req2;
  *req2.mutable_policy() = MakeDefaultPolicy(3);
  req2.set_client_nonce(kNonce);

  auto resp2 = session_mgr_->CreateSession(req2);
  EXPECT_FALSE(resp2.ok());
  EXPECT_EQ(resp2.status().code(), absl::StatusCode::kInvalidArgument);
}

// =============================================================================
// Test Suite C: JoinSession Idempotency & Capacity
// =============================================================================

TEST_F(SessionManagerTest, JoinSession_NonceReplayReturnsSameToken) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create_resp = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create_resp.ok());

  const std::string kJoinNonce = "6ba7b810-9dad-11d1-80b4-00c04fd430c8";
  // Convert to valid UUIDv4: version 4, variant 8
  const std::string kUuidV4 = "6ba7b810-9dad-41d1-80b4-00c04fd430c8";

  JoinSessionRequest join_req;
  join_req.set_invitation_token(create_resp->invitation_token());
  join_req.set_client_nonce(kUuidV4);

  auto join1 = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join1.ok());
  EXPECT_FALSE(join1->participant_token().empty());

  // Replay JoinSession with exact same nonce
  auto join2 = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join2.ok());
  EXPECT_EQ(join1->participant_token(), join2->participant_token());
}

TEST_F(SessionManagerTest, JoinSession_InvalidNonceRejected) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create_resp = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create_resp.ok());

  JoinSessionRequest join_req;
  join_req.set_invitation_token(create_resp->invitation_token());
  join_req.set_client_nonce("not-a-uuid");

  auto join_resp = session_mgr_->JoinSession(join_req);
  EXPECT_FALSE(join_resp.ok());
  EXPECT_EQ(join_resp.status().code(), absl::StatusCode::kInvalidArgument);
}

TEST_F(SessionManagerTest, JoinSession_ReplayAfterSessionSealed) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create_resp = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create_resp.ok());

  const std::string kJoinerNonce = "d3b07384-d113-4ec6-9f44-93ff3504380f";
  JoinSessionRequest join_req;
  join_req.set_invitation_token(create_resp->invitation_token());
  join_req.set_client_nonce(kJoinerNonce);

  auto join1 = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join1.ok());

  // Accept policy for both creator and joiner -> session transitions to SEALED
  AcceptPolicyRequest accept_req;
  accept_req.set_participant_token(join1->participant_token());
  auto accept_resp = session_mgr_->AcceptPolicy(accept_req);
  ASSERT_TRUE(accept_resp.ok());
  EXPECT_EQ(accept_resp->state(), SEALED);

  // Participant retries JoinSession after session reached SEALED
  auto join_replay = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join_replay.ok());
  EXPECT_EQ(join_replay->participant_token(), join1->participant_token());
  EXPECT_EQ(join_replay->state(), SEALED);
}

TEST_F(SessionManagerTest, JoinSession_CapacityEnforced) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create_resp = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create_resp.ok());

  // Join participant 2
  JoinSessionRequest join1_req;
  join1_req.set_invitation_token(create_resp->invitation_token());
  join1_req.set_client_nonce("11111111-1111-4111-8111-111111111111");
  auto join1 = session_mgr_->JoinSession(join1_req);
  ASSERT_TRUE(join1.ok());

  // Try to join participant 3 into a 2-person session
  JoinSessionRequest join2_req;
  join2_req.set_invitation_token(create_resp->invitation_token());
  join2_req.set_client_nonce("22222222-2222-4222-8222-222222222222");
  auto join2 = session_mgr_->JoinSession(join2_req);
  EXPECT_FALSE(join2.ok());
  EXPECT_EQ(join2.status().code(), absl::StatusCode::kPermissionDenied);
}

// =============================================================================
// Test Suite D: AcceptPolicy Idempotency
// =============================================================================

TEST_F(SessionManagerTest, AcceptPolicy_IdempotentReplay) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create_resp = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create_resp.ok());

  JoinSessionRequest join_req;
  join_req.set_invitation_token(create_resp->invitation_token());
  auto join_resp = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join_resp.ok());

  AcceptPolicyRequest accept_req;
  accept_req.set_participant_token(join_resp->participant_token());

  auto accept1 = session_mgr_->AcceptPolicy(accept_req);
  ASSERT_TRUE(accept1.ok());
  EXPECT_EQ(accept1->state(), SEALED);

  // Duplicate AcceptPolicy call
  auto accept2 = session_mgr_->AcceptPolicy(accept_req);
  ASSERT_TRUE(accept2.ok());
  EXPECT_EQ(accept2->state(), SEALED);
}

// =============================================================================
// Test Suite E: SubmitInput Idempotency & Content Protection
// =============================================================================

TEST_F(SessionManagerTest, SubmitInput_IdempotentReplayExactMatch) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create_resp = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create_resp.ok());

  JoinSessionRequest join_req;
  join_req.set_invitation_token(create_resp->invitation_token());
  auto join_resp = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join_resp.ok());

  AcceptPolicyRequest accept_req;
  accept_req.set_participant_token(join_resp->participant_token());
  ASSERT_TRUE(session_mgr_->AcceptPolicy(accept_req).ok());

  const std::string kInput = R"({"slots": ["2026-07-15T10:00:00Z"]})";

  SubmitInputRequest submit_req;
  submit_req.set_participant_token(create_resp->participant_token());
  submit_req.set_input_json(kInput);

  auto submit1 = session_mgr_->SubmitInput(submit_req);
  ASSERT_TRUE(submit1.ok());
  EXPECT_EQ(submit1->state(), SEALED);
  EXPECT_EQ(submit1->remaining_inputs(), 1);

  // Replay exact same SubmitInput
  auto submit2 = session_mgr_->SubmitInput(submit_req);
  ASSERT_TRUE(submit2.ok());
  EXPECT_EQ(submit2->state(), SEALED);
  EXPECT_EQ(submit2->remaining_inputs(), 1);
}

TEST_F(SessionManagerTest, SubmitInput_ContentMismatchRejected) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create_resp = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create_resp.ok());

  JoinSessionRequest join_req;
  join_req.set_invitation_token(create_resp->invitation_token());
  auto join_resp = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join_resp.ok());

  AcceptPolicyRequest accept_req;
  accept_req.set_participant_token(join_resp->participant_token());
  ASSERT_TRUE(session_mgr_->AcceptPolicy(accept_req).ok());

  SubmitInputRequest submit1_req;
  submit1_req.set_participant_token(create_resp->participant_token());
  submit1_req.set_input_json(R"({"slots": ["2026-07-15T10:00:00Z"]})");
  ASSERT_TRUE(session_mgr_->SubmitInput(submit1_req).ok());

  // Replay with different content
  SubmitInputRequest submit2_req;
  submit2_req.set_participant_token(create_resp->participant_token());
  submit2_req.set_input_json(R"({"slots": ["2026-07-16T14:00:00Z"]})");

  auto submit2 = session_mgr_->SubmitInput(submit2_req);
  EXPECT_FALSE(submit2.ok());
  EXPECT_EQ(submit2.status().code(), absl::StatusCode::kFailedPrecondition);
}

TEST_F(SessionManagerTest, SubmitInput_UnsealedRejection) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create_resp = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create_resp.ok());

  // Try to submit input while session is OPEN (before participant
  // joined/accepted)
  SubmitInputRequest submit_req;
  submit_req.set_participant_token(create_resp->participant_token());
  submit_req.set_input_json(R"({"slots": ["2026-07-15T10:00:00Z"]})");

  auto submit = session_mgr_->SubmitInput(submit_req);
  EXPECT_FALSE(submit.ok());
  EXPECT_EQ(submit.status().code(), absl::StatusCode::kFailedPrecondition);
}

TEST_F(SessionManagerTest, SubmitInput_SchemaViolationRejected) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create_resp = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create_resp.ok());

  JoinSessionRequest join_req;
  join_req.set_invitation_token(create_resp->invitation_token());
  auto join_resp = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join_resp.ok());

  AcceptPolicyRequest accept_req;
  accept_req.set_participant_token(join_resp->participant_token());
  ASSERT_TRUE(session_mgr_->AcceptPolicy(accept_req).ok());

  // Schema expects object with "slots" array. Send integer instead.
  SubmitInputRequest submit_req;
  submit_req.set_participant_token(create_resp->participant_token());
  submit_req.set_input_json(R"({"invalid_field": 123})");

  auto submit = session_mgr_->SubmitInput(submit_req);
  EXPECT_FALSE(submit.ok());
  EXPECT_EQ(submit.status().code(), absl::StatusCode::kInvalidArgument);
}

// =============================================================================
// Test Suite F: Capability Tokens & Status
// =============================================================================

TEST_F(SessionManagerTest, CapabilityTokens_InvalidTokenRejected) {
  GetSessionStatusRequest req;
  req.set_participant_token("invalid-random-token");
  auto status_or = session_mgr_->GetSessionStatus(req);
  EXPECT_FALSE(status_or.ok());
  EXPECT_EQ(status_or.status().code(), absl::StatusCode::kPermissionDenied);
}

TEST_F(SessionManagerTest, GetSessionStatus_ReflectsLifecycle) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create_resp = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create_resp.ok());

  GetSessionStatusRequest status_req;
  status_req.set_participant_token(create_resp->participant_token());

  auto status1 = session_mgr_->GetSessionStatus(status_req);
  ASSERT_TRUE(status1.ok());
  EXPECT_EQ(status1->state(), OPEN);
  EXPECT_EQ(status1->participants_joined(), 1);
  EXPECT_EQ(status1->participants_accepted(), 1);
  EXPECT_EQ(status1->inputs_received(), 0);

  JoinSessionRequest join_req;
  join_req.set_invitation_token(create_resp->invitation_token());
  auto join_resp = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join_resp.ok());

  auto status2 = session_mgr_->GetSessionStatus(status_req);
  ASSERT_TRUE(status2.ok());
  EXPECT_EQ(status2->participants_joined(), 2);
  EXPECT_EQ(status2->participants_accepted(), 1);
}

// =============================================================
// Test Suite: Resource Limits (F7, F8)
// =============================================================

TEST_F(SessionManagerTest, CreateSession_MaxParticipantsEnforced) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  req.mutable_policy()->set_expected_participants(101);
  auto resp = session_mgr_->CreateSession(req);
  EXPECT_FALSE(resp.ok());
  EXPECT_EQ(resp.status().code(), absl::StatusCode::kInvalidArgument);
}

TEST_F(SessionManagerTest, CreateSession_MinParticipantsEnforced) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  req.mutable_policy()->set_expected_participants(1);
  auto resp = session_mgr_->CreateSession(req);
  EXPECT_FALSE(resp.ok());
  EXPECT_EQ(resp.status().code(), absl::StatusCode::kInvalidArgument);
}

TEST_F(SessionManagerTest, CreateSession_MaxTimeoutEnforced) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  req.mutable_policy()->set_timeout_seconds(86401);  // > 24h
  auto resp = session_mgr_->CreateSession(req);
  EXPECT_FALSE(resp.ok());
  EXPECT_EQ(resp.status().code(), absl::StatusCode::kInvalidArgument);
}

TEST_F(SessionManagerTest, CreateSession_MaxTimeoutAtBoundary) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  req.mutable_policy()->set_timeout_seconds(86400);  // exactly 24h
  auto resp = session_mgr_->CreateSession(req);
  EXPECT_TRUE(resp.ok()) << resp.status();
}

TEST_F(SessionManagerTest, CreateSession_InvalidPolicyClass) {
  CreateSessionRequest req;
  req.mutable_policy()->set_policy_class("NonExistentPolicy");
  req.mutable_policy()->set_expected_participants(2);
  auto resp = session_mgr_->CreateSession(req);
  EXPECT_FALSE(resp.ok());
  EXPECT_EQ(resp.status().code(), absl::StatusCode::kInvalidArgument);
}

// =============================================================
// Test Suite: Input Size Limit
// =============================================================

TEST_F(SessionManagerTest, SubmitInput_MaxSizeEnforced) {
  // Create + join + accept to get to SEALED.
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create.ok());

  JoinSessionRequest join_req;
  join_req.set_invitation_token(create->invitation_token());
  auto join = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join.ok());

  AcceptPolicyRequest accept_req;
  accept_req.set_participant_token(join->participant_token());
  auto accept = session_mgr_->AcceptPolicy(accept_req);
  ASSERT_TRUE(accept.ok());

  // Submit oversized input (> 64KB).
  std::string huge_json = "{\"slots\":[\"" + std::string(70000, 'X') + "\"]}";
  SubmitInputRequest submit_req;
  submit_req.set_participant_token(join->participant_token());
  submit_req.set_input_json(huge_json);
  auto submit = session_mgr_->SubmitInput(submit_req);
  EXPECT_FALSE(submit.ok());
  EXPECT_EQ(submit.status().code(), absl::StatusCode::kInvalidArgument);
}

// =============================================================
// Test Suite: Full Lifecycle to SEALED
// =============================================================

TEST_F(SessionManagerTest, FullLifecycle_AcceptTransitionsToSealed) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(2);
  auto create = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create.ok());
  EXPECT_EQ(create->state(), OPEN);

  JoinSessionRequest join_req;
  join_req.set_invitation_token(create->invitation_token());
  auto join = session_mgr_->JoinSession(join_req);
  ASSERT_TRUE(join.ok());

  // Joiner accepts policy.
  AcceptPolicyRequest accept_req;
  accept_req.set_participant_token(join->participant_token());
  auto accept = session_mgr_->AcceptPolicy(accept_req);
  ASSERT_TRUE(accept.ok());
  // Both participants accepted -> SEALED.
  EXPECT_EQ(accept->state(), SEALED);
}

TEST_F(SessionManagerTest, AcceptPolicy_PartialAcceptStaysOpen) {
  CreateSessionRequest create_req;
  *create_req.mutable_policy() = MakeDefaultPolicy(3);
  auto create = session_mgr_->CreateSession(create_req);
  ASSERT_TRUE(create.ok());

  // Join two more participants.
  JoinSessionRequest join1;
  join1.set_invitation_token(create->invitation_token());
  auto j1 = session_mgr_->JoinSession(join1);
  ASSERT_TRUE(j1.ok());

  JoinSessionRequest join2;
  join2.set_invitation_token(create->invitation_token());
  auto j2 = session_mgr_->JoinSession(join2);
  ASSERT_TRUE(j2.ok());

  // Only first joiner accepts — still OPEN (need 3 accepts).
  AcceptPolicyRequest accept1;
  accept1.set_participant_token(j1->participant_token());
  auto a1 = session_mgr_->AcceptPolicy(accept1);
  ASSERT_TRUE(a1.ok());
  EXPECT_EQ(a1->state(), OPEN);

  // Second joiner accepts — now all 3 accepted -> SEALED.
  AcceptPolicyRequest accept2;
  accept2.set_participant_token(j2->participant_token());
  auto a2 = session_mgr_->AcceptPolicy(accept2);
  ASSERT_TRUE(a2.ok());
  EXPECT_EQ(a2->state(), SEALED);
}

// =============================================================
// Test Suite: Auth Uniformity
// =============================================================

TEST_F(SessionManagerTest, Auth_AllRpcsRejectBadToken) {
  // Verify every authenticated RPC returns PERMISSION_DENIED
  // for an invalid token.
  const std::string bad = "invalid-token-does-not-exist";

  AcceptPolicyRequest accept;
  accept.set_participant_token(bad);
  auto a = session_mgr_->AcceptPolicy(accept);
  EXPECT_EQ(a.status().code(), absl::StatusCode::kPermissionDenied);

  SubmitInputRequest submit;
  submit.set_participant_token(bad);
  submit.set_input_json("{}");
  auto s = session_mgr_->SubmitInput(submit);
  EXPECT_EQ(s.status().code(), absl::StatusCode::kPermissionDenied);

  GetResultRequest result;
  result.set_participant_token(bad);
  auto r = session_mgr_->GetResult(result);
  EXPECT_EQ(r.status().code(), absl::StatusCode::kPermissionDenied);

  GetSessionStatusRequest status;
  status.set_participant_token(bad);
  auto st = session_mgr_->GetSessionStatus(status);
  EXPECT_EQ(st.status().code(), absl::StatusCode::kPermissionDenied);
}

// =============================================================
// Test Suite: Default Timeout
// =============================================================

TEST_F(SessionManagerTest, CreateSession_DefaultTimeout) {
  CreateSessionRequest req;
  *req.mutable_policy() = MakeDefaultPolicy();
  req.mutable_policy()->set_timeout_seconds(0);  // Use default
  auto resp = session_mgr_->CreateSession(req);
  ASSERT_TRUE(resp.ok());

  GetSessionStatusRequest status_req;
  status_req.set_participant_token(resp->participant_token());
  auto status = session_mgr_->GetSessionStatus(status_req);
  ASSERT_TRUE(status.ok());
  EXPECT_EQ(status->state(), OPEN);
}

// =============================================================
// MockLlamaEngine: Configurable mock for testing
// ProcessSessionAsync end-to-end without real GGUF models.
// =============================================================

class MockLlamaEngine : public LlamaEngine {
 public:
  // Each call to Generate() returns the next response from the
  // queue. If the queue is exhausted, returns InternalError.
  void AddResponse(absl::StatusOr<std::string> response) {
    responses_.push_back(std::move(response));
  }

  // Convenience: replace all responses at once.
  void SetResponses(std::vector<absl::StatusOr<std::string>> resps) {
    responses_ = std::move(resps);
    response_idx_ = 0;
  }

  // Optional delay per Generate() call, for testing
  // CALCULATING timeout.
  void set_delay(std::chrono::milliseconds d) { delay_ = d; }

  absl::StatusOr<std::string> Generate(const std::string& prompt,
                                       int max_tokens) override {
    last_prompt_ = prompt;
    last_max_tokens_ = max_tokens;
    ++call_count_;
    if (delay_.count() > 0) {
      std::this_thread::sleep_for(delay_);
    }
    if (response_idx_ < responses_.size()) {
      return responses_[response_idx_++];
    }
    return absl::InternalError("MockLlamaEngine: no more responses");
  }

  std::string last_prompt_;
  int last_max_tokens_ = 0;
  int call_count_ = 0;

 private:
  std::vector<absl::StatusOr<std::string>> responses_;
  size_t response_idx_ = 0;
  std::chrono::milliseconds delay_{0};
};

// Test fixture that uses MockLlamaEngine instead of nullptr.
class SessionManagerWithEngineTest : public ::testing::Test {
 protected:
  void SetUp() override {
    PolicyDefinition def;
    def.prompt_template = "Overlap for {num_participants}: {inputs}";
    def.default_input_schema_json = kTestInputSchema;
    def.default_output_schema_json = kTestOutputSchema;
    registry_.RegisterPolicy("ScheduleOverlap", std::move(def));
  }

  // Create a SessionManager with the mock engine.
  std::unique_ptr<SessionManager> MakeManager() {
    return std::make_unique<SessionManager>(&engine_, &registry_);
  }

  // Create a SessionManager with custom Options.
  std::unique_ptr<SessionManager> MakeManagerWithOptions(
      const SessionManager::Options& opts) {
    return std::make_unique<SessionManager>(&engine_, &registry_, opts);
  }

  // Helper: drive a 2-participant session to CALCULATING
  // state by creating, joining, accepting, and submitting
  // inputs for both participants. Returns the creator's
  // participant token for polling.
  std::string DriveToCalculating(SessionManager* mgr) {
    CreateSessionRequest create_req;
    auto* p = create_req.mutable_policy();
    p->set_policy_class("ScheduleOverlap");
    p->set_expected_participants(2);
    p->set_timeout_seconds(300);
    auto create = mgr->CreateSession(create_req);
    EXPECT_TRUE(create.ok()) << create.status();

    JoinSessionRequest join_req;
    join_req.set_invitation_token(create->invitation_token());
    auto join = mgr->JoinSession(join_req);
    EXPECT_TRUE(join.ok()) << join.status();

    // Both accept (creator auto-accepted).
    AcceptPolicyRequest accept_req;
    accept_req.set_participant_token(join->participant_token());
    auto accept = mgr->AcceptPolicy(accept_req);
    EXPECT_TRUE(accept.ok()) << accept.status();
    EXPECT_EQ(accept->state(), SEALED);

    // Creator submits input.
    SubmitInputRequest submit1;
    submit1.set_participant_token(create->participant_token());
    submit1.set_input_json(R"({"slots": ["2026-07-15T10:00:00Z"]})");
    auto s1 = mgr->SubmitInput(submit1);
    EXPECT_TRUE(s1.ok()) << s1.status();
    EXPECT_EQ(s1->remaining_inputs(), 1);

    // Joiner submits input — triggers CALCULATING.
    SubmitInputRequest submit2;
    submit2.set_participant_token(join->participant_token());
    submit2.set_input_json(R"({"slots": ["2026-07-15T14:00:00Z"]})");
    auto s2 = mgr->SubmitInput(submit2);
    EXPECT_TRUE(s2.ok()) << s2.status();
    EXPECT_EQ(s2->state(), CALCULATING);

    return create->participant_token();
  }

  // Poll GetResult until session is no longer CALCULATING
  // (max 50 iterations with 100ms sleep).
  GetResultResponse PollUntilDone(SessionManager* mgr,
                                  const std::string& token) {
    for (int i = 0; i < 50; ++i) {
      GetResultRequest req;
      req.set_participant_token(token);
      auto resp = mgr->GetResult(req);
      if (resp.ok() && resp->state() != CALCULATING) {
        return *resp;
      }
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    GetResultResponse empty;
    empty.set_state(CALCULATING);
    return empty;
  }

  MockLlamaEngine engine_;
  PolicyRegistry registry_;
};

// =============================================================
// Test Suite: Async LLM Processing (Tasks 2.2–2.5)
// =============================================================

TEST_F(SessionManagerWithEngineTest,
       ProcessAsync_HappyPath_ProducesClosedWithResult) {
  // Engine returns valid JSON matching output schema.
  engine_.AddResponse(R"({"overlap": "2026-07-15T14:00:00Z"})");

  auto mgr = MakeManager();
  std::string token = DriveToCalculating(mgr.get());

  auto result = PollUntilDone(mgr.get(), token);
  EXPECT_EQ(result.state(), CLOSED);
  EXPECT_FALSE(result.result_json().empty());
  // Verify the result contains the overlap key.
  EXPECT_NE(result.result_json().find("overlap"), std::string::npos);
  // Verify engine was called with max_tokens=4096.
  EXPECT_EQ(engine_.last_max_tokens_, 4096);
  // Verify prompt contains template substitutions.
  EXPECT_NE(engine_.last_prompt_.find("Overlap for 2"), std::string::npos);
  // Verify delimiter injection (random hex suffix).
  EXPECT_NE(engine_.last_prompt_.find("PARTICIPANT_1_INPUT_BEGIN_"),
            std::string::npos);
}

TEST_F(SessionManagerWithEngineTest, ProcessAsync_CodeFenceStripping) {
  // Engine wraps valid JSON in markdown code fences.
  engine_.AddResponse(
      "```json\n"
      "{\"overlap\": \"2026-07-15T14:00:00Z\"}\n"
      "```");

  auto mgr = MakeManager();
  std::string token = DriveToCalculating(mgr.get());

  auto result = PollUntilDone(mgr.get(), token);
  EXPECT_EQ(result.state(), CLOSED);
  // Result should be the unwrapped JSON.
  EXPECT_NE(result.result_json().find("overlap"), std::string::npos);
  EXPECT_EQ(result.result_json().find("```"), std::string::npos);
}

TEST_F(SessionManagerWithEngineTest, ProcessAsync_RetrySucceedsOnThirdAttempt) {
  // First two attempts: one fails JSON parse, one violates
  // the pattern constraint on 'overlap'.
  engine_.AddResponse("not valid json at all");
  engine_.AddResponse(R"({"overlap": "INVALID_NOT_DIGITS"})");
  // Third attempt returns valid output.
  engine_.AddResponse(R"({"overlap": "2026-07-15T10:00:00Z"})");

  auto mgr = MakeManager();
  std::string token = DriveToCalculating(mgr.get());

  auto result = PollUntilDone(mgr.get(), token);
  EXPECT_EQ(result.state(), CLOSED);
  EXPECT_EQ(engine_.call_count_, 3);
}

TEST_F(SessionManagerWithEngineTest,
       ProcessAsync_AllRetriesFail_SessionAborted) {
  // All 3 attempts violate the overlap pattern.
  engine_.AddResponse(R"({"overlap": "INVALID_1"})");
  engine_.AddResponse(R"({"overlap": "INVALID_2"})");
  engine_.AddResponse(R"({"overlap": "INVALID_3"})");

  auto mgr = MakeManager();
  std::string token = DriveToCalculating(mgr.get());

  auto result = PollUntilDone(mgr.get(), token);
  EXPECT_EQ(result.state(), ABORTED);
  EXPECT_EQ(result.error_code(), LLM_GENERATION_FAILED);
  EXPECT_EQ(engine_.call_count_, 3);
}

TEST_F(SessionManagerWithEngineTest, ProcessAsync_NullEngine_SessionAborted) {
  // Use nullptr engine to test the null-engine guard.
  auto mgr = std::make_unique<SessionManager>(
      /*engine=*/nullptr, &registry_);

  CreateSessionRequest create_req;
  auto* p = create_req.mutable_policy();
  p->set_policy_class("ScheduleOverlap");
  p->set_expected_participants(2);
  p->set_timeout_seconds(300);
  auto create = mgr->CreateSession(create_req);
  ASSERT_TRUE(create.ok());

  JoinSessionRequest join_req;
  join_req.set_invitation_token(create->invitation_token());
  auto join = mgr->JoinSession(join_req);
  ASSERT_TRUE(join.ok());

  AcceptPolicyRequest accept_req;
  accept_req.set_participant_token(join->participant_token());
  ASSERT_TRUE(mgr->AcceptPolicy(accept_req).ok());

  SubmitInputRequest s1;
  s1.set_participant_token(create->participant_token());
  s1.set_input_json(R"({"slots": ["2026-07-15T10:00:00Z"]})");
  ASSERT_TRUE(mgr->SubmitInput(s1).ok());

  SubmitInputRequest s2;
  s2.set_participant_token(join->participant_token());
  s2.set_input_json(R"({"slots": ["2026-07-15T14:00:00Z"]})");
  auto submit2 = mgr->SubmitInput(s2);
  ASSERT_TRUE(submit2.ok());
  EXPECT_EQ(submit2->state(), CALCULATING);

  // Poll for result — should abort with null engine.
  auto result = PollUntilDone(mgr.get(), create->participant_token());
  EXPECT_EQ(result.state(), ABORTED);
  EXPECT_EQ(result.error_code(), LLM_GENERATION_FAILED);
}

// =============================================================
// Test Suite: Schema Override Prevention (Task 2.11)
// =============================================================

TEST_F(SessionManagerWithEngineTest,
       CreateSession_ClientSchemaOverrideIgnored) {
  engine_.AddResponse(R"({"overlap": "2026-07-15T10:00:00Z"})");

  auto mgr = MakeManager();

  // Create session with client-provided schema override.
  CreateSessionRequest req;
  auto* p = req.mutable_policy();
  p->set_policy_class("ScheduleOverlap");
  p->set_expected_participants(2);
  p->set_timeout_seconds(300);
  // Client tries to inject a permissive schema.
  p->set_input_schema_json("{}");
  p->set_output_schema_json("{}");

  auto create = mgr->CreateSession(req);
  ASSERT_TRUE(create.ok());

  // Verify the schema was overwritten by checking that
  // invalid input (missing required "slots") is rejected.
  JoinSessionRequest join_req;
  join_req.set_invitation_token(create->invitation_token());
  auto join = mgr->JoinSession(join_req);
  ASSERT_TRUE(join.ok());

  AcceptPolicyRequest accept_req;
  accept_req.set_participant_token(join->participant_token());
  ASSERT_TRUE(mgr->AcceptPolicy(accept_req).ok());

  // Submit input violating the REAL schema (not the
  // client's permissive "{}").
  SubmitInputRequest submit;
  submit.set_participant_token(create->participant_token());
  submit.set_input_json(R"({"invalid_field": 123})");
  auto result = mgr->SubmitInput(submit);
  EXPECT_FALSE(result.ok());
  EXPECT_EQ(result.status().code(), absl::StatusCode::kInvalidArgument);
}

// =============================================================
// Test Suite: kMaxSessions Capacity (Task 2.10)
// =============================================================

TEST_F(SessionManagerWithEngineTest, CreateSession_MaxSessionsCapacity) {
  auto mgr = MakeManager();

  // Create 1000 sessions (the maximum).
  for (int i = 0; i < 1000; ++i) {
    CreateSessionRequest req;
    auto* p = req.mutable_policy();
    p->set_policy_class("ScheduleOverlap");
    p->set_expected_participants(2);
    p->set_timeout_seconds(300);
    auto resp = mgr->CreateSession(req);
    ASSERT_TRUE(resp.ok()) << "Session " << i << ": " << resp.status();
  }

  // Session 1001 should fail with RESOURCE_EXHAUSTED.
  CreateSessionRequest req;
  auto* p = req.mutable_policy();
  p->set_policy_class("ScheduleOverlap");
  p->set_expected_participants(2);
  p->set_timeout_seconds(300);
  auto resp = mgr->CreateSession(req);
  EXPECT_FALSE(resp.ok());
  EXPECT_EQ(resp.status().code(), absl::StatusCode::kResourceExhausted);
}

// =============================================================
// Test Suite: Timeout Enforcement (Tasks 2.6, 2.7)
// =============================================================

TEST_F(SessionManagerWithEngineTest, TimeoutInOpenState_JoinTimeout) {
  auto mgr = MakeManager();
  CreateSessionRequest req;
  auto* p = req.mutable_policy();
  p->set_policy_class("ScheduleOverlap");
  p->set_expected_participants(2);
  p->set_timeout_seconds(1);

  auto create = mgr->CreateSession(req);
  ASSERT_TRUE(create.ok());

  // Wait for timeout to expire.
  std::this_thread::sleep_for(std::chrono::milliseconds(1100));

  // Use GetResult with the creator's participant token
  // (not JoinSession — JoinSession returns a uniform
  // PERMISSION_DENIED for non-OPEN sessions to prevent
  // session enumeration).
  GetResultRequest get_req;
  get_req.set_participant_token(create->participant_token());
  auto result = mgr->GetResult(get_req);
  ASSERT_TRUE(result.ok());
  EXPECT_EQ(result->state(), ABORTED);
  EXPECT_EQ(result->error_code(), JOIN_TIMEOUT);
}

TEST_F(SessionManagerWithEngineTest, TimeoutInSealedState_InputTimeout) {
  auto mgr = MakeManager();
  CreateSessionRequest req;
  auto* p = req.mutable_policy();
  p->set_policy_class("ScheduleOverlap");
  p->set_expected_participants(2);
  p->set_timeout_seconds(1);
  auto create = mgr->CreateSession(req);
  ASSERT_TRUE(create.ok());

  JoinSessionRequest join;
  join.set_invitation_token(create->invitation_token());
  auto joined = mgr->JoinSession(join);
  ASSERT_TRUE(joined.ok());

  // Both accept policy to reach SEALED.
  AcceptPolicyRequest acc1;
  acc1.set_participant_token(create->participant_token());
  ASSERT_TRUE(mgr->AcceptPolicy(acc1).ok());

  AcceptPolicyRequest acc2;
  acc2.set_participant_token(joined->participant_token());
  auto sealed = mgr->AcceptPolicy(acc2);
  ASSERT_TRUE(sealed.ok());
  EXPECT_EQ(sealed->state(), SEALED);

  // Wait for timeout to expire.
  std::this_thread::sleep_for(std::chrono::milliseconds(1100));

  // SubmitInput triggers CheckAndEnforceTimeout.
  SubmitInputRequest sub;
  sub.set_participant_token(create->participant_token());
  sub.set_input_json(R"({"slots": ["2026-07-15T10:00:00Z"]})");
  auto res = mgr->SubmitInput(sub);
  EXPECT_FALSE(res.ok());

  // Verify via GetResult.
  GetResultRequest get_req;
  get_req.set_participant_token(create->participant_token());
  auto result = mgr->GetResult(get_req);
  ASSERT_TRUE(result.ok());
  EXPECT_EQ(result->state(), ABORTED);
  EXPECT_EQ(result->error_code(), INPUT_TIMEOUT);
}

// =============================================================
// Test Suite: GC Immediate Eviction (Task 2.8)
// =============================================================

TEST_F(SessionManagerWithEngineTest, GcEvictsTimeoutSessionsImmediately) {
  auto mgr = MakeManager();
  CreateSessionRequest req;
  auto* p = req.mutable_policy();
  p->set_policy_class("ScheduleOverlap");
  p->set_expected_participants(2);
  p->set_timeout_seconds(1);
  auto create1 = mgr->CreateSession(req);
  ASSERT_TRUE(create1.ok());

  // Wait for timeout to expire.
  std::this_thread::sleep_for(std::chrono::milliseconds(1100));

  // CreateSession triggers SweepTerminalSessions,
  // which calls CheckAndEnforceTimeout on all sessions
  // (transitioning OPEN→ABORTED with JOIN_TIMEOUT),
  // then immediately evicts JOIN_TIMEOUT sessions.
  auto create2 = mgr->CreateSession(req);
  ASSERT_TRUE(create2.ok());

  // The old session was swept: its participant token
  // is no longer in the reverse index, so Authenticate
  // returns PERMISSION_DENIED (uniform anti-enumeration
  // error — NOT kNotFound).
  GetSessionStatusRequest stat;
  stat.set_participant_token(create1->participant_token());
  auto stat_res = mgr->GetSessionStatus(stat);
  EXPECT_FALSE(stat_res.ok());
  EXPECT_EQ(stat_res.status().code(), absl::StatusCode::kPermissionDenied);
}

// =============================================================
// Test Suite: GC Retention (Task 2.9) — requires Options
// =============================================================

TEST_F(SessionManagerWithEngineTest, GcRetainsNormalClosedSessionsUntilExpiry) {
  // Use Options to set retention to 1 second.
  SessionManager::Options opts;
  opts.terminal_retention_seconds = 1;
  auto mgr = MakeManagerWithOptions(opts);

  // Provide a valid LLM response so session reaches
  // CLOSED (not ABORTED). Schema expects overlap as
  // a string matching ^[0-9TZ:-]+$.
  engine_.SetResponses({R"({"overlap": "2026-07-15T10:00:00Z"})"});

  std::string token = DriveToCalculating(mgr.get());

  // Poll until CLOSED.
  PollUntilDone(mgr.get(), token);
  {
    GetResultRequest req;
    req.set_participant_token(token);
    auto res = mgr->GetResult(req);
    ASSERT_TRUE(res.ok());
    ASSERT_EQ(res->state(), CLOSED);
  }

  // Session just closed — GC should NOT evict it yet.
  // Trigger a sweep via CreateSession.
  CreateSessionRequest req2;
  auto* p2 = req2.mutable_policy();
  p2->set_policy_class("ScheduleOverlap");
  p2->set_expected_participants(2);
  p2->set_timeout_seconds(300);
  ASSERT_TRUE(mgr->CreateSession(req2).ok());

  // Token should still be valid.
  {
    GetResultRequest req;
    req.set_participant_token(token);
    auto res = mgr->GetResult(req);
    EXPECT_TRUE(res.ok()) << "Session should still be retained: "
                          << res.status();
  }

  // Now wait for retention to expire.
  std::this_thread::sleep_for(std::chrono::milliseconds(1100));

  // Trigger sweep again.
  ASSERT_TRUE(mgr->CreateSession(req2).ok());

  // Token should be gone — session was swept.
  {
    GetResultRequest req;
    req.set_participant_token(token);
    auto res = mgr->GetResult(req);
    EXPECT_FALSE(res.ok());
    EXPECT_EQ(res.status().code(), absl::StatusCode::kPermissionDenied);
  }
}

// =============================================================
// Test Suite: CALCULATING Timeout (Task 2.6b)
// =============================================================

TEST_F(SessionManagerWithEngineTest, CalculatingTimeout_LlmGenerationFailed) {
  // Set max_calculating_seconds=1 and make the engine
  // block for 2s so the async thread is still running
  // when GetResult checks the timeout.
  SessionManager::Options opts;
  opts.max_calculating_seconds = 1;
  auto mgr = MakeManagerWithOptions(opts);

  // Engine will block for 2s (exceeding the 1s cap).
  engine_.set_delay(std::chrono::milliseconds(2000));
  engine_.SetResponses({R"({"overlap": "2026-07-15T10:00:00Z"})"});

  std::string token = DriveToCalculating(mgr.get());

  // Wait just past the 1s CALCULATING timeout,
  // but not the full 2s engine delay.
  std::this_thread::sleep_for(std::chrono::milliseconds(1100));

  // GetResult triggers CheckAndEnforceTimeout,
  // which sees CALCULATING state > 1s → ABORTED
  // with LLM_GENERATION_FAILED.
  GetResultRequest req;
  req.set_participant_token(token);
  auto result = mgr->GetResult(req);
  ASSERT_TRUE(result.ok());
  EXPECT_EQ(result->state(), ABORTED);
  EXPECT_EQ(result->error_code(), LLM_GENERATION_FAILED);

  // Reset delay for other tests.
  engine_.set_delay(std::chrono::milliseconds(0));
}

}  // namespace
}  // namespace ztab
