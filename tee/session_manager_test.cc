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

#include <string>

#include "absl/status/status.h"
#include "gmock/gmock.h"
#include "gtest/gtest.h"
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

}  // namespace
}  // namespace ztab
