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

"""Unit tests for ITA verifier and verifier factory."""

import sys
import unittest
from unittest import mock

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import serialization
import datetime

from agent import ita_verifier
from agent import verifier_factory
from agent.client import noop_verifier


def _make_self_signed_ec_cert_pem() -> bytes:
    """Create a self-signed EC certificate and return PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "test"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(
            datetime.datetime.utcnow()
        )
        .not_valid_after(
            datetime.datetime.utcnow()
            + datetime.timedelta(days=1)
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


# -- Good claims template for ITA verifier tests. --
_GOOD_CLAIMS = {
    "iss": "https://portal.trustauthority.intel.com",
    "aud": "ztab_tls",
    "hwmodel": "GCP_INTEL_TDX",
    "secboot": True,
    "dbgstat": "disabled-since-boot",
    "eat_nonce": "expected-nonce",
}


def _claims(**overrides):
    """Return a copy of _GOOD_CLAIMS with overrides."""
    c = dict(_GOOD_CLAIMS)
    c.update(overrides)
    return c


def _patch_jwt_and_jwks(claims_dict):
    """Return a stack of patches for JWT / JWKS mocks.

    The returned patches mock out:
      - _fetch_jwks  -> {'keys': [...]}
      - jwt.get_unverified_header -> {'kid':'k1','alg':'RS256'}
      - jwt.PyJWKSet.from_dict -> keyset with key_id='k1'
      - jwt.decode -> claims_dict
    """
    mock_key = mock.MagicMock()
    mock_key.key_id = "k1"
    mock_key.key = "fake-key"

    mock_keyset = mock.MagicMock()
    mock_keyset.keys = [mock_key]

    patches = [
        mock.patch.object(
            ita_verifier,
            "_fetch_jwks",
            return_value={"keys": [{"kid": "k1"}]},
        ),
        mock.patch(
            "jwt.get_unverified_header",
            return_value={"kid": "k1", "alg": "RS256"},
        ),
        mock.patch(
            "jwt.PyJWKSet.from_dict",
            return_value=mock_keyset,
        ),
        mock.patch(
            "jwt.decode",
            return_value=claims_dict,
        ),
    ]
    return patches


class Base64HelperTest(unittest.TestCase):
    """Tests for base64url encoding/decoding helpers."""

    def test_base64url_decode_nopad(self):
        result = ita_verifier._base64url_decode("SGVsbG8")
        self.assertEqual(result, b"Hello")

    def test_base64url_encode_nopad(self):
        result = ita_verifier._base64url_encode_nopad(
            b"Hello"
        )
        self.assertEqual(result, "SGVsbG8")


class PubkeyHashTest(unittest.TestCase):
    """Tests for _compute_pubkey_hash_b64url."""

    def test_compute_pubkey_hash_b64url(self):
        cert_pem = _make_self_signed_ec_cert_pem()
        h = ita_verifier._compute_pubkey_hash_b64url(
            cert_pem
        )
        self.assertIsInstance(h, str)
        self.assertTrue(len(h) > 0)
        # Base64url without padding: no '=' chars.
        self.assertNotIn("=", h)


class ItaVerifierClaimTest(unittest.TestCase):
    """Tests for ITA verifier claim validation logic.

    Each test creates a verifier via create_ita_verifier,
    then patches JWT/JWKS so only the claim-checking code
    path under test executes.
    """

    def _run_verifier(self, claims_dict, **kwargs):
        """Build verifier, apply patches, call it."""
        # Reset JWKS cache between tests.
        ita_verifier._JWKS_CACHE["data"] = None
        ita_verifier._JWKS_CACHE["timestamp"] = 0

        extra_patches = kwargs.pop(
            "extra_patches", []
        )
        verifier = ita_verifier.create_ita_verifier(
            **kwargs
        )
        patches = _patch_jwt_and_jwks(claims_dict)
        patches.extend(extra_patches)

        for p in patches:
            p.start()
        try:
            nonce_patch = kwargs.get(
                "_nonce_patch", None
            )
            return verifier("token", b"cert-pem")
        finally:
            for p in patches:
                p.stop()

    def test_ita_verifier_rejects_bad_issuer(self):
        result = self._run_verifier(
            _claims(iss="https://evil.com")
        )
        self.assertFalse(result)

    def test_ita_verifier_rejects_bad_hwmodel(self):
        result = self._run_verifier(
            _claims(hwmodel="INVALID")
        )
        self.assertFalse(result)

    def test_ita_verifier_rejects_debug_enabled(self):
        result = self._run_verifier(
            _claims(dbgstat="enabled"),
            require_debug_disabled=True,
        )
        self.assertFalse(result)

    def test_ita_verifier_rejects_nonce_mismatch(self):
        nonce_mock = mock.patch.object(
            ita_verifier,
            "_compute_pubkey_hash_b64url",
            return_value="expected-nonce",
        )
        result = self._run_verifier(
            _claims(eat_nonce="wrong"),
            extra_patches=[nonce_mock],
        )
        self.assertFalse(result)


class VerifierFactoryTest(unittest.TestCase):
    """Tests for verifier_factory.get_verifier."""

    def test_get_verifier_noop(self):
        v = verifier_factory.get_verifier("noop")
        # Can't use assertIs because module import paths
        # differ. Verify it behaves like noop_verifier.
        self.assertTrue(callable(v))
        self.assertTrue(v("token", b"cert"))

    def test_get_verifier_unknown_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            verifier_factory.get_verifier("unknown")
        self.assertEqual(ctx.exception.code, 1)

import time


class JwksCachingTest(unittest.TestCase):
    """Tests for JWKS 24-hour TTL caching."""

    def setUp(self):
        """Reset JWKS cache before each test."""
        ita_verifier._JWKS_CACHE["data"] = None
        ita_verifier._JWKS_CACHE["timestamp"] = 0

    def tearDown(self):
        """Reset JWKS cache after each test."""
        ita_verifier._JWKS_CACHE["data"] = None
        ita_verifier._JWKS_CACHE["timestamp"] = 0

    @mock.patch.object(
        ita_verifier,
        "_compute_pubkey_hash_b64url",
        return_value="expected-nonce",
    )
    @mock.patch.object(
        ita_verifier, "_fetch_jwks",
        return_value={"keys": [{"kid": "k1"}]},
    )
    @mock.patch("agent.ita_verifier.time.time")
    def test_jwks_cache_hit_within_ttl(
        self, mock_time, mock_fetch, mock_nonce
    ):
        """Cached JWKS within TTL skips HTTP fetch."""
        now = 1000000.0
        cached_keys = {"keys": [{"kid": "k1"}]}

        # Pre-populate cache 100 seconds ago.
        ita_verifier._JWKS_CACHE["data"] = (
            cached_keys
        )
        ita_verifier._JWKS_CACHE["timestamp"] = (
            now - 100
        )
        mock_time.return_value = now

        # Build verifier and call it. The internal
        # get_jwks() should use the cache, not fetch.
        patches = _patch_jwt_and_jwks(
            _claims(eat_nonce="expected-nonce")
        )
        # Remove _fetch_jwks patch from helper since
        # we already patched it above.
        patches = [
            p for p in patches
            if "_fetch_jwks" not in str(p)
        ]
        for p in patches:
            p.start()
        try:
            verifier = (
                ita_verifier.create_ita_verifier()
            )
            result = verifier("tok", b"cert")
        finally:
            for p in patches:
                p.stop()

        # _fetch_jwks should NOT be called because
        # cache is fresh.
        mock_fetch.assert_not_called()

    @mock.patch.object(
        ita_verifier,
        "_compute_pubkey_hash_b64url",
        return_value="expected-nonce",
    )
    @mock.patch.object(
        ita_verifier, "_fetch_jwks",
        return_value={"keys": [{"kid": "k1"}]},
    )
    @mock.patch("agent.ita_verifier.time.time")
    def test_jwks_cache_miss_after_ttl(
        self, mock_time, mock_fetch, mock_nonce
    ):
        """Expired JWKS cache triggers HTTP fetch."""
        now = 1000000.0
        stale_keys = {"keys": [{"kid": "old"}]}

        # Cache expired > 86400 seconds ago.
        ita_verifier._JWKS_CACHE["data"] = (
            stale_keys
        )
        ita_verifier._JWKS_CACHE["timestamp"] = (
            now - 86401
        )
        mock_time.return_value = now

        # Build and call verifier.
        mock_key = mock.MagicMock()
        mock_key.key_id = "k1"
        mock_key.key = "fake-key"
        mock_keyset = mock.MagicMock()
        mock_keyset.keys = [mock_key]

        patches = [
            mock.patch(
                "jwt.get_unverified_header",
                return_value={
                    "kid": "k1",
                    "alg": "RS256",
                },
            ),
            mock.patch(
                "jwt.PyJWKSet.from_dict",
                return_value=mock_keyset,
            ),
            mock.patch(
                "jwt.decode",
                return_value=_claims(
                    eat_nonce="expected-nonce"
                ),
            ),
        ]
        for p in patches:
            p.start()
        try:
            verifier = (
                ita_verifier.create_ita_verifier()
            )
            verifier("tok", b"cert")
        finally:
            for p in patches:
                p.stop()

        # _fetch_jwks SHOULD be called because TTL
        # expired.
        mock_fetch.assert_called()


class KeyRotationTest(unittest.TestCase):
    """Test JWKS key rotation triggers refetch."""

    def setUp(self):
        ita_verifier._JWKS_CACHE["data"] = None
        ita_verifier._JWKS_CACHE["timestamp"] = 0

    def tearDown(self):
        ita_verifier._JWKS_CACHE["data"] = None
        ita_verifier._JWKS_CACHE["timestamp"] = 0

    @mock.patch.object(
        ita_verifier,
        "_compute_pubkey_hash_b64url",
        return_value="expected-nonce",
    )
    @mock.patch.object(
        ita_verifier, "_fetch_jwks"
    )
    @mock.patch(
        "jwt.get_unverified_header",
        return_value={
            "kid": "k2", "alg": "RS256"
        },
    )
    @mock.patch("jwt.decode")
    @mock.patch("jwt.PyJWKSet.from_dict")
    def test_key_rotation_forces_refetch(
        self,
        mock_from_dict,
        mock_decode,
        mock_header,
        mock_fetch,
        mock_nonce,
    ):
        """Missing kid triggers JWKS refetch."""
        # First fetch: keys without k2.
        mock_key1 = mock.MagicMock()
        mock_key1.key_id = "k1"
        mock_key1.key = "fake-key-1"
        keyset_no_k2 = mock.MagicMock()
        keyset_no_k2.keys = [mock_key1]

        # Second fetch: keys with k2.
        mock_key2 = mock.MagicMock()
        mock_key2.key_id = "k2"
        mock_key2.key = "fake-key-2"
        keyset_with_k2 = mock.MagicMock()
        keyset_with_k2.keys = [
            mock_key1, mock_key2
        ]

        mock_fetch.return_value = {
            "keys": [{"kid": "k1"}]
        }
        mock_from_dict.side_effect = [
            keyset_no_k2,
            keyset_with_k2,
        ]
        mock_decode.return_value = _claims(
            eat_nonce="expected-nonce"
        )

        verifier = (
            ita_verifier.create_ita_verifier()
        )
        verifier("tok", b"cert")

        # _fetch_jwks called twice: initial +
        # rotation refetch.
        self.assertEqual(
            mock_fetch.call_count, 2
        )


class ContainerDigestTest(unittest.TestCase):
    """Tests for container image digest check."""

    def setUp(self):
        ita_verifier._JWKS_CACHE["data"] = None
        ita_verifier._JWKS_CACHE["timestamp"] = 0

    def tearDown(self):
        ita_verifier._JWKS_CACHE["data"] = None
        ita_verifier._JWKS_CACHE["timestamp"] = 0

    def _run(self, digest_arg, actual_digest):
        """Helper: run verifier with digest args."""
        claims_dict = _claims(
            eat_nonce="expected-nonce",
            submods={
                "container": {
                    "image_digest": actual_digest,
                },
            },
        )
        nonce_patch = mock.patch.object(
            ita_verifier,
            "_compute_pubkey_hash_b64url",
            return_value="expected-nonce",
        )
        patches = _patch_jwt_and_jwks(claims_dict)
        patches.append(nonce_patch)

        verifier = (
            ita_verifier.create_ita_verifier(
                expected_image_digest=digest_arg,
            )
        )
        for p in patches:
            p.start()
        try:
            return verifier("tok", b"cert")
        finally:
            for p in patches:
                p.stop()

    def test_digest_match_passes(self):
        """Matching digest returns True."""
        result = self._run(
            "sha256:abc123", "sha256:abc123"
        )
        self.assertTrue(result)

    def test_digest_mismatch_fails(self):
        """Mismatched digest returns False."""
        result = self._run(
            "sha256:abc123", "sha256:wrong"
        )
        self.assertFalse(result)

    def test_empty_digest_skips(self):
        """Empty expected_image_digest skips
        digest check."""
        claims_dict = _claims(
            eat_nonce="expected-nonce",
            submods={"container": {}},
        )
        nonce_patch = mock.patch.object(
            ita_verifier,
            "_compute_pubkey_hash_b64url",
            return_value="expected-nonce",
        )
        patches = _patch_jwt_and_jwks(claims_dict)
        patches.append(nonce_patch)

        verifier = (
            ita_verifier.create_ita_verifier(
                expected_image_digest="",
            )
        )
        for p in patches:
            p.start()
        try:
            result = verifier("tok", b"cert")
        finally:
            for p in patches:
                p.stop()

        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
