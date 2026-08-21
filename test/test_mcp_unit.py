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
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.

"""Unit tests for MCP server pure-logic functions.

No live gRPC connections are used; all external dependencies
are mocked.
"""

import base64
import json
import unittest
from unittest import mock

from agent import mcp_server


class DecodeJwtPayloadTest(unittest.TestCase):
  """Tests for decode_jwt_payload."""

  def test_decode_jwt_payload_valid(self):
    """Valid base64url JWT payload is decoded correctly."""
    payload = {"sub": "agent-1", "iss": "ztab"}
    b64 = (
        base64.urlsafe_b64encode(
            json.dumps(payload).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    token = f"header.{b64}.signature"
    result = mcp_server.decode_jwt_payload(token)
    self.assertEqual(result, payload)

  def test_decode_jwt_payload_invalid(self):
    """Garbage base64 payload returns None."""
    token = "header.!!!invalid-base64!!!.sig"
    result = mcp_server.decode_jwt_payload(token)
    self.assertIsNone(result)

  def test_decode_jwt_payload_too_few_parts(self):
    """Single-segment string (no dots) returns None."""
    result = mcp_server.decode_jwt_payload("x")
    self.assertIsNone(result)


class ValidateRequiredArgsTest(unittest.TestCase):
  """Tests for _validate_required_args."""

  def test_validate_required_args_missing(self):
    """Missing required arg returns an error dict."""
    err = mcp_server._validate_required_args(
        "ztab_test_connection", {}
    )
    self.assertIsNotNone(err)
    self.assertEqual(err["status"], "error")
    self.assertIn("message", err)

  def test_validate_required_args_present(self):
    """All required args present returns None."""
    result = mcp_server._validate_required_args(
        "ztab_test_connection",
        {"message": "hello"},
    )
    self.assertIsNone(result)


class ValidateBackendsTest(unittest.TestCase):
  """Tests for _validate_backends."""

  def test_validate_backends_valid(self):
    """Well-formed config raises no exception."""
    config = {
        "backends": [
            {
                "backend_id": "b1",
                "host": "localhost",
                "port": 8000,
                "verifier": "noop",
            }
        ]
    }
    # Should not raise.
    mcp_server._validate_backends(config, "<test>")

  def test_validate_backends_missing_field(self):
    """Backend missing 'host' raises ValueError."""
    config = {
        "backends": [
            {
                "backend_id": "bad",
                "port": 8000,
                "verifier": "noop",
            }
        ]
    }
    with self.assertRaises(ValueError) as ctx:
      mcp_server._validate_backends(
          config, "<test>"
      )
    self.assertIn("host", str(ctx.exception))


class HandleRequestTest(unittest.TestCase):
  """Tests for the handle_request dispatcher."""

  def test_handle_request_initialize(self):
    """'initialize' returns protocol version."""
    result = mcp_server.handle_request(
        "initialize", {}
    )
    self.assertEqual(
        result["protocolVersion"],
        mcp_server.PROTOCOL_VERSION,
    )
    self.assertIn("serverInfo", result)

  def test_handle_request_tools_list(self):
    """'tools/list' returns the TOOLS catalogue."""
    result = mcp_server.handle_request(
        "tools/list", {}
    )
    self.assertEqual(result["tools"], mcp_server.TOOLS)

  def test_handle_request_unknown_method(self):
    """Unknown method returns the _NOT_HANDLED sentinel."""
    result = mcp_server.handle_request(
        "bogus/method", {}
    )
    self.assertIs(result, mcp_server._NOT_HANDLED)

  def test_handle_request_notification(self):
    """'notifications/initialized' returns None."""
    result = mcp_server.handle_request(
        "notifications/initialized", {}
    )
    self.assertIsNone(result)


class RunListBackendsTest(unittest.TestCase):
  """Tests for run_list_backends."""

  @mock.patch.object(mcp_server, "_get_backends")
  def test_run_list_backends_no_config(
      self, mock_get
  ):
    """Returns error when no backends are configured."""
    mock_get.return_value = None
    result = mcp_server.run_list_backends({})
    self.assertEqual(result["status"], "error")

  @mock.patch.object(mcp_server, "_get_backends")
  def test_run_list_backends_with_config(
      self, mock_get
  ):
    """Returns sanitised backend list (no host/port)."""
    mock_get.return_value = {
        "default_backend": "prod",
        "backends": [
            {
                "backend_id": "prod",
                "name": "Production",
                "description": "Prod TEE",
                "host": "tee.internal",
                "port": 443,
                "verifier": "gce",
            }
        ],
    }
    result = mcp_server.run_list_backends({})
    self.assertEqual(result["status"], "success")
    self.assertEqual(len(result["backends"]), 1)

    b = result["backends"][0]
    self.assertEqual(b["backend_id"], "prod")
    self.assertEqual(
        b["security_level"], "production"
    )
    # Host/port must NOT leak to the agent.
    self.assertNotIn("host", b)
    self.assertNotIn("port", b)

import os


class RunCreateSessionTest(unittest.TestCase):
  """Tests for run_create_session."""

  @mock.patch.object(mcp_server, "_get_cached_stub")
  def test_run_create_session_injects_creator_token(
      self, mock_cached_stub
  ):
    """creator_token from backend config is sent as
    gRPC metadata."""
    mock_channel = mock.MagicMock()
    mock_stub = mock.MagicMock()

    # Simulate a CreateSession response.
    mock_resp = mock.MagicMock()
    mock_resp.invitation_token = "inv-tok"
    mock_resp.participant_token = "pt-tok"
    mock_resp.state = 1  # WAITING_FOR_PARTICIPANTS
    mock_stub.CreateSession.return_value = mock_resp

    backend_info = {
        "backend_id": "test-be",
        "host": "localhost",
        "port": 8000,
        "verifier": "noop",
        "creator_token": "secret123",
    }
    mock_cached_stub.return_value = (
        mock_channel,
        mock_stub,
        backend_info,
    )

    args = {
        "policy_class": "ScheduleOverlap",
        "expected_participants": 2,
    }
    result = mcp_server.run_create_session(args)

    self.assertEqual(result["status"], "success")

    # Verify CreateSession was called with the
    # creator_token in metadata.
    call_kwargs = (
        mock_stub.CreateSession.call_args
    )
    metadata = call_kwargs[1].get(
        "metadata",
        call_kwargs[0][1]
        if len(call_kwargs[0]) > 1
        else None,
    )
    self.assertIn(
        ("x-ztab-creator-token", "secret123"),
        metadata,
    )


class BackendsFileEnvOverrideTest(unittest.TestCase):
  """Tests for _get_backends env-var override (A67)."""

  @mock.patch("builtins.open", mock.mock_open(
      read_data=json.dumps({
          "backends": [{
              "backend_id": "env-be",
              "host": "env-host",
              "port": 9999,
              "verifier": "noop",
          }],
      })
  ))
  @mock.patch.object(mcp_server, "os")
  def test_backends_file_env_override(
      self, mock_os
  ):
    """ZTAB_BACKENDS_FILE overrides the default
    ~/.ztab/backends.json path."""
    override_path = "/tmp/test.json"
    mock_os.environ.get.return_value = (
        override_path
    )
    mock_os.stat.return_value = mock.MagicMock(
        st_mtime_ns=99999
    )

    # Reset module-level cache so _get_backends
    # re-reads from disk.
    mcp_server._backends_mtime_ns = 0
    mcp_server._backends_config = None

    config = mcp_server._get_backends()

    # Verify os.stat was called with the env path.
    mock_os.stat.assert_called_once_with(
        override_path
    )
    self.assertIsNotNone(config)
    self.assertEqual(
        config["backends"][0]["backend_id"],
        "env-be",
    )


if __name__ == "__main__":
  unittest.main()
