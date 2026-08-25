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

"""Unit tests for ZTAB client library (client.py).

Tests noop_verifier and ZtabChannel without requiring a live
TLS server by mocking _fetch_server_cert_pem,
extract_attestation_token, and grpc internals.
"""

import sys
import os
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), '..', 'agent'))

from client import ZtabChannel, noop_verifier


FAKE_CERT_PEM = b"-----BEGIN CERTIFICATE-----\nfake\n"


class NoopVerifierTest(unittest.TestCase):
    """Tests for noop_verifier."""

    def test_noop_verifier_fails_without_test_env(self):
        """noop_verifier raises RuntimeError when ZTAB_TEST_ENVIRONMENT is unset."""
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(RuntimeError) as ctx:
                noop_verifier('token', b'cert')
            self.assertIn("strictly forbidden", str(ctx.exception))

    def test_noop_verifier_returns_true_with_test_env(self):
        """noop_verifier returns True when ZTAB_TEST_ENVIRONMENT=1."""
        with mock.patch.dict(os.environ, {"ZTAB_TEST_ENVIRONMENT": "1"}):
            self.assertTrue(noop_verifier('token', b'cert'))
            self.assertTrue(noop_verifier('', b''))
            self.assertTrue(noop_verifier('abc', b'\x00\xff'))
            self.assertTrue(noop_verifier('long' * 100, b'x' * 999))


class ZtabChannelTest(unittest.TestCase):
    """Tests for ZtabChannel (connect, close, context)."""

    def setUp(self):
        self.env_patcher = mock.patch.dict(
            os.environ, {"ZTAB_TEST_ENVIRONMENT": "1"}
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()

    def test_channel_init_requires_verifier(self):
        """ZtabChannel raises ValueError if verifier is None."""
        with self.assertRaises(ValueError) as ctx:
            ZtabChannel('localhost', 8000, verifier=None)
        self.assertIn("A verifier is required", str(ctx.exception))

    @mock.patch('client.grpc.secure_channel')
    @mock.patch('client.grpc.ssl_channel_credentials')
    @mock.patch('client.extract_attestation_token')
    @mock.patch('client._fetch_server_cert_pem')
    def test_channel_connect_calls_verifier(
        self,
        mock_fetch,
        mock_extract,
        mock_ssl_creds,
        mock_secure_chan,
    ):
        """Verifier is called with (token, cert_pem)."""
        mock_fetch.return_value = FAKE_CERT_PEM
        mock_extract.return_value = 'test-jwt'
        mock_ssl_creds.return_value = mock.sentinel.creds
        mock_secure_chan.return_value = mock.MagicMock()

        verifier = mock.MagicMock(return_value=True)
        ch = ZtabChannel('localhost', 8000, verifier=verifier)
        ch.connect()

        verifier.assert_called_once_with(
            'test-jwt', FAKE_CERT_PEM
        )

    @mock.patch('client.extract_attestation_token')
    @mock.patch('client._fetch_server_cert_pem')
    def test_channel_connect_raises_on_no_cert_extension(
        self, mock_fetch, mock_extract,
    ):
        """RuntimeError when attestation extension is missing."""
        mock_fetch.return_value = FAKE_CERT_PEM
        mock_extract.return_value = None

        ch = ZtabChannel(
            'localhost', 8000,
            verifier=noop_verifier,
        )
        with self.assertRaises(RuntimeError):
            ch.connect()

    @mock.patch('client.extract_attestation_token')
    @mock.patch('client._fetch_server_cert_pem')
    def test_channel_connect_raises_on_verification_failure(
        self, mock_fetch, mock_extract,
    ):
        """RuntimeError when verifier returns False."""
        mock_fetch.return_value = FAKE_CERT_PEM
        mock_extract.return_value = 'jwt'

        failing_verifier = lambda tok, cert: False
        ch = ZtabChannel(
            'localhost', 8000, verifier=failing_verifier,
        )
        with self.assertRaises(RuntimeError):
            ch.connect()

    def test_channel_close(self):
        """close() calls grpc_channel.close(), sets to None."""
        ch = ZtabChannel(
            'localhost', 8000,
            verifier=noop_verifier,
        )
        mock_grpc_ch = mock.MagicMock()
        ch.grpc_channel = mock_grpc_ch

        ch.close()

        mock_grpc_ch.close.assert_called_once()
        self.assertIsNone(ch.grpc_channel)

    @mock.patch('client.grpc.secure_channel')
    @mock.patch('client.grpc.ssl_channel_credentials')
    @mock.patch('client.extract_attestation_token')
    @mock.patch('client._fetch_server_cert_pem')
    def test_channel_context_manager(
        self,
        mock_fetch,
        mock_extract,
        mock_ssl_creds,
        mock_secure_chan,
    ):
        """__enter__ calls connect, __exit__ calls close."""
        mock_fetch.return_value = FAKE_CERT_PEM
        mock_extract.return_value = 'jwt-ctx'
        mock_ssl_creds.return_value = mock.sentinel.creds
        mock_grpc_ch = mock.MagicMock()
        mock_secure_chan.return_value = mock_grpc_ch

        with ZtabChannel(
            'localhost', 8000,
            verifier=noop_verifier,
        ) as ch:
            self.assertIsNotNone(ch.grpc_channel)

        mock_grpc_ch.close.assert_called_once()
        self.assertIsNone(ch.grpc_channel)


if __name__ == '__main__':
    unittest.main()
