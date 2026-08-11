#!/usr/bin/env python3
# Copyright 2026 Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for ZTAB Python client, CLI, and MCP server admission control."""

import argparse
import json
import unittest
from unittest import mock

from agent import cli
from agent import mcp_server
from agent.pb2 import session_manager_pb2


class McpServerSchemaTest(unittest.TestCase):
  """Tests for MCP server tool schemas."""

  def test_create_session_schema_hides_client_nonce(self):
    """client_nonce was removed from agent-visible schema (Entry 16).
    Nonces are auto-generated internally by the MCP server."""
    create_tool = next(
        t for t in mcp_server.TOOLS if t["name"] == "ztab_create_session"
    )
    props = create_tool["inputSchema"]["properties"]
    self.assertNotIn("client_nonce", props)

  def test_join_session_schema_hides_client_nonce(self):
    """client_nonce was removed from agent-visible schema (Entry 16).
    Nonces are auto-generated internally by the MCP server."""
    join_tool = next(
        t for t in mcp_server.TOOLS if t["name"] == "ztab_join_session"
    )
    props = join_tool["inputSchema"]["properties"]
    self.assertNotIn("client_nonce", props)


class McpServerHandlerTest(unittest.TestCase):
  """Tests for MCP server tool execution handlers."""

  @mock.patch.object(mcp_server, "_get_cached_stub")
  @mock.patch.object(mcp_server, "_get_backend_info")
  def test_run_create_session_injects_creator_token_and_nonce(
      self, mock_backend_info, mock_cached_stub
  ):
    mock_backend_info.return_value = {
        "host": "localhost",
        "port": 8000,
        "creator_token": "secret-creator-token-123",
    }
    mock_stub = mock.MagicMock()
    mock_channel = mock.MagicMock()
    mock_stub.CreateSession.return_value = session_manager_pb2.CreateSessionResponse(
        invitation_token="inv_tok_123",
        participant_token="part_tok_123",
        state=session_manager_pb2.OPEN,
    )
    mock_cached_stub.return_value = (
        mock_channel, mock_stub,
        mock_backend_info.return_value,
    )

    args = {
        "backend": "test-tee",
        "policy_class": "ScheduleOverlap",
        "expected_participants": 2,
        "client_nonce": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    }
    result = mcp_server.run_create_session(args)

    self.assertEqual(result["status"], "success")
    self.assertEqual(result["invitation_token"], "inv_tok_123")
    self.assertEqual(result["participant_token"], "part_tok_123")

    mock_stub.CreateSession.assert_called_once()
    call_args = mock_stub.CreateSession.call_args
    req = call_args[0][0]
    metadata = call_args[1].get("metadata")

    self.assertEqual(req.client_nonce, "f47ac10b-58cc-4372-a567-0e02b2c3d479")
    self.assertEqual(req.policy.policy_class, "ScheduleOverlap")
    self.assertEqual(req.policy.expected_participants, 2)
    self.assertEqual(
        metadata, [("x-ztab-creator-token", "secret-creator-token-123")]
    )

  @mock.patch.object(mcp_server, "_get_cached_stub")
  @mock.patch.object(mcp_server, "_get_backend_info")
  def test_run_create_session_without_creator_token(
      self, mock_backend_info, mock_cached_stub
  ):
    mock_backend_info.return_value = {
        "host": "localhost",
        "port": 8000,
    }
    mock_stub = mock.MagicMock()
    mock_channel = mock.MagicMock()
    mock_stub.CreateSession.return_value = session_manager_pb2.CreateSessionResponse(
        invitation_token="inv_tok_456",
        participant_token="part_tok_456",
        state=session_manager_pb2.OPEN,
    )
    mock_cached_stub.return_value = (
        mock_channel, mock_stub,
        mock_backend_info.return_value,
    )

    args = {
        "policy_class": "ScheduleOverlap",
        "expected_participants": 2,
    }
    result = mcp_server.run_create_session(args)

    self.assertEqual(result["status"], "success")
    call_args = mock_stub.CreateSession.call_args
    metadata = call_args[1].get("metadata")
    self.assertIsNone(metadata)

  @mock.patch.object(mcp_server, "_get_cached_stub")
  @mock.patch.object(mcp_server, "_get_backend_info")
  def test_run_join_session_forwards_nonce(
      self, mock_backend_info, mock_cached_stub
  ):
    mock_backend_info.return_value = {"host": "localhost", "port": 8000}
    mock_stub = mock.MagicMock()
    mock_channel = mock.MagicMock()
    policy = session_manager_pb2.SessionPolicy(
        policy_class="ScheduleOverlap", expected_participants=2
    )
    mock_stub.JoinSession.return_value = session_manager_pb2.JoinSessionResponse(
        participant_token="joined_part_tok",
        state=session_manager_pb2.OPEN,
        policy=policy,
    )
    mock_cached_stub.return_value = (
        mock_channel, mock_stub,
        mock_backend_info.return_value,
    )

    args = {
        "invitation_token": "inv_test_token",
        "client_nonce": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    }
    result = mcp_server.run_join_session(args)

    self.assertEqual(result["status"], "success")
    self.assertEqual(result["participant_token"], "joined_part_tok")

    call_args = mock_stub.JoinSession.call_args
    req = call_args[0][0]
    self.assertEqual(req.invitation_token, "inv_test_token")
    self.assertEqual(req.client_nonce, "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")


class CliParsingTest(unittest.TestCase):
  """Tests for CLI arguments and subcommand execution."""

  def test_create_session_arg_parsing(self):
    parser = cli.build_parser()
    args = parser.parse_args([
        "create-session",
        "--verifier", "noop",
        "--host", "localhost",
        "--port", "8000",
        "--policy", "ScheduleOverlap",
        "--participants", "3",
        "--creator-token", "my-secret-token",
        "--client-nonce", "c9a646d3-9c61-4cd7-9f59-e1b630d7d80f",
    ])
    self.assertEqual(args.host, "localhost")
    self.assertEqual(args.port, 8000)
    self.assertEqual(args.policy, "ScheduleOverlap")
    self.assertEqual(args.participants, 3)
    self.assertEqual(args.creator_token, "my-secret-token")
    self.assertEqual(args.client_nonce, "c9a646d3-9c61-4cd7-9f59-e1b630d7d80f")

  def test_join_session_arg_parsing(self):
    parser = cli.build_parser()
    args = parser.parse_args([
        "join-session",
        "--verifier", "noop",
        "--invitation-token", "inv-12345",
        "--client-nonce", "e2d83b38-6b22-4a7b-83c9-04d9c7921a2b",
    ])
    self.assertEqual(args.invitation_token, "inv-12345")
    self.assertEqual(args.client_nonce, "e2d83b38-6b22-4a7b-83c9-04d9c7921a2b")

  @mock.patch.object(cli, "_make_stub")
  def test_cmd_create_session_forwards_token_and_nonce(self, mock_make_stub):
    mock_stub = mock.MagicMock()
    mock_channel = mock.MagicMock()
    mock_stub.CreateSession.return_value = session_manager_pb2.CreateSessionResponse(
        invitation_token="inv_tok_cli",
        participant_token="part_tok_cli",
        state=session_manager_pb2.OPEN,
    )
    mock_make_stub.return_value = (mock_stub, mock_channel)

    parser = cli.build_parser()
    args = parser.parse_args([
        "create-session",
        "--verifier", "noop",
        "--policy", "ScheduleOverlap",
        "--participants", "2",
        "--creator-token", "cli-creator-token-789",
        "--client-nonce", "f47ac10b-58cc-4372-a567-0e02b2c3d479",
    ])
    cli.cmd_create_session(args)

    mock_stub.CreateSession.assert_called_once()
    call_args = mock_stub.CreateSession.call_args
    req = call_args[0][0]
    metadata = call_args[1].get("metadata")

    self.assertEqual(req.client_nonce, "f47ac10b-58cc-4372-a567-0e02b2c3d479")
    self.assertEqual(req.policy.policy_class, "ScheduleOverlap")
    self.assertEqual(
        metadata, [("x-ztab-creator-token", "cli-creator-token-789")]
    )
    mock_channel.close.assert_called_once()

  @mock.patch.object(cli, "_make_stub")
  def test_cmd_join_session_forwards_nonce(self, mock_make_stub):
    mock_stub = mock.MagicMock()
    mock_channel = mock.MagicMock()
    mock_stub.JoinSession.return_value = session_manager_pb2.JoinSessionResponse(
        participant_token="part_tok_joined",
        state=session_manager_pb2.OPEN,
        policy=session_manager_pb2.SessionPolicy(
            policy_class="ScheduleOverlap", expected_participants=2
        ),
    )
    mock_make_stub.return_value = (mock_stub, mock_channel)

    parser = cli.build_parser()
    args = parser.parse_args([
        "join-session",
        "--verifier", "noop",
        "--invitation-token", "inv-tok-999",
        "--client-nonce", "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11",
    ])
    cli.cmd_join_session(args)

    mock_stub.JoinSession.assert_called_once()
    call_args = mock_stub.JoinSession.call_args
    req = call_args[0][0]

    self.assertEqual(req.invitation_token, "inv-tok-999")
    self.assertEqual(req.client_nonce, "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
    mock_channel.close.assert_called_once()


if __name__ == "__main__":
  unittest.main()
