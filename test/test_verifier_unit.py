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

"""Unit tests for ITA verifier, verifier factory, and policy classes."""

import copy
import datetime
import os
import sys
import time
import unittest
from unittest import mock

# Setup python path to import agent modules.
_SCRIPT_DIR = os.path.dirname(
    os.path.abspath(__file__)
)
_ZTAB_ROOT = os.path.dirname(_SCRIPT_DIR)
_AGENT_DIR = os.path.join(_ZTAB_ROOT, "agent")
sys.path.insert(0, _ZTAB_ROOT)
sys.path.insert(0, _AGENT_DIR)

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import serialization

from agent import ita_verifier
from agent import verifier_factory

from agent.verifier_policy import (
    ItaPolicy,
    NoopPolicy,
    VerifierPolicy,
)


def _make_self_signed_ec_cert_pem() -> bytes:
    """Create a self-signed EC cert and return PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(
            NameOID.COMMON_NAME, "test"
        ),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(
            x509.random_serial_number()
        )
        .not_valid_before(
            datetime.datetime.utcnow()
        )
        .not_valid_after(
            datetime.datetime.utcnow()
            + datetime.timedelta(days=1)
        )
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(
        serialization.Encoding.PEM
    )


# Default ITA policy used by most tests.
_DEFAULT_POLICY = ItaPolicy(
    expected_image_digests=frozenset(
        ["sha256:abc123testdigest"]
    ),
)

# Good claims template matching ALL unconditional
# constants (ITA issuer, INTEL_TDX, secboot=True,
# CONFIDENTIAL_SPACE, gcp_compliant_cvm) plus a
# valid swversion >= 260500.
_GOOD_CLAIMS = {
    "iss": ita_verifier.ITA_ISSUER,
    "aud": ita_verifier.EXPECTED_AUDIENCE,
    "hwmodel": ita_verifier.EXPECTED_HWMODEL,
    "secboot": True,
    "dbgstat": "disabled-since-boot",
    "eat_nonce": "expected-nonce",
    "swname": ita_verifier.EXPECTED_SWNAME,
    "swversion": ["260500"],
    "submods": {
        "container": {
            "image_digest": (
                "sha256:abc123testdigest"
            ),
        },
        "confidential_space": {
            "support_attributes": [],
            "monitoring_enabled": {
                "memory": False,
            },
        },
    },
    "tdx": {
        "cvm_compliance_status": (
            ita_verifier.EXPECTED_CVM_COMPLIANCE
        ),
    },
}


def _claims(**overrides):
    """Return a deep copy of _GOOD_CLAIMS with overrides."""
    c = copy.deepcopy(_GOOD_CLAIMS)
    c.update(overrides)
    return c


def _patch_jwt_and_jwks(claims_dict):
    """Return patches for JWT / JWKS mocks."""
    mock_key = mock.MagicMock()
    mock_key.key_id = "k1"
    mock_key.key = "fake-key"

    mock_keyset = mock.MagicMock()
    mock_keyset.keys = [mock_key]

    return [
        mock.patch.object(
            ita_verifier,
            "_fetch_jwks",
            return_value={
                "keys": [{"kid": "k1"}]
            },
        ),
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
            return_value=claims_dict,
        ),
    ]


# ─── Helper Tests ────────────────────────────────────


class Base64HelperTest(unittest.TestCase):

    def test_base64url_decode_nopad(self):
        result = ita_verifier._base64url_decode(
            "SGVsbG8"
        )
        self.assertEqual(result, b"Hello")

    def test_base64url_encode_nopad(self):
        result = (
            ita_verifier._base64url_encode_nopad(
                b"Hello"
            )
        )
        self.assertEqual(result, "SGVsbG8")


class PubkeyHashTest(unittest.TestCase):

    def test_compute_pubkey_hash_b64url(self):
        cert_pem = _make_self_signed_ec_cert_pem()
        h = (
            ita_verifier
            ._compute_pubkey_hash_b64url(cert_pem)
        )
        self.assertIsInstance(h, str)
        self.assertTrue(len(h) > 0)
        self.assertNotIn("=", h)


# ─── ITA Verifier Claim Tests ────────────────────────


class ItaVerifierClaimTest(unittest.TestCase):
    """Tests for ITA verifier claim validation.

    Each test patches JWT/JWKS so only the
    claim-checking code path under test executes.
    """

    def _run(
        self,
        claims_dict,
        policy=None,
        extra_patches=None,
    ):
        """Build verifier, apply patches, call it."""
        ita_verifier._JWKS_CACHE["data"] = None
        ita_verifier._JWKS_CACHE["timestamp"] = 0

        if policy is None:
            policy = _DEFAULT_POLICY
        if extra_patches is None:
            extra_patches = []

        verifier = ita_verifier.create_ita_verifier(
            policy
        )
        patches = _patch_jwt_and_jwks(claims_dict)
        # Default: mock cert→nonce so tests that
        # reach key-binding don't crash on the
        # dummy b"cert-pem" bytestring.
        patches.append(
            mock.patch.object(
                ita_verifier,
                "_compute_pubkey_hash_b64url",
                return_value="expected-nonce",
            )
        )
        patches.extend(extra_patches)

        for p in patches:
            p.start()
        try:
            return verifier("token", b"cert-pem")
        finally:
            for p in patches:
                p.stop()

    # --- Unconditional checks ---

    def test_rejects_bad_issuer(self):
        self.assertFalse(
            self._run(
                _claims(iss="https://evil.com")
            )
        )

    def test_rejects_gca_issuer(self):
        """GCA issuer is rejected by ITA verifier."""
        self.assertFalse(
            self._run(
                _claims(
                    iss=ita_verifier.GCA_ISSUER
                )
            )
        )

    def test_rejects_wrong_hwmodel(self):
        self.assertFalse(
            self._run(
                _claims(hwmodel="INVALID")
            )
        )

    def test_rejects_gcp_intel_tdx_hwmodel(self):
        """GCP_INTEL_TDX is rejected (ITA uses INTEL_TDX)."""
        self.assertFalse(
            self._run(
                _claims(hwmodel="GCP_INTEL_TDX")
            )
        )

    def test_rejects_secboot_false(self):
        """secboot=False is always rejected."""
        self.assertFalse(
            self._run(_claims(secboot=False))
        )

    def test_rejects_bad_swname(self):
        claims = _claims()
        claims["swname"] = "WRONG"
        self.assertFalse(self._run(claims))

    def test_rejects_bad_cvm_compliance(self):
        claims = _claims()
        claims["tdx"][
            "cvm_compliance_status"
        ] = "non_compliant"
        self.assertFalse(self._run(claims))

    # --- Debug gating ---

    def test_rejects_debug_enabled(self):
        self.assertFalse(
            self._run(
                _claims(dbgstat="enabled")
            )
        )

    def test_rejects_debug_support_attr(self):
        claims = _claims()
        claims["submods"]["confidential_space"][
            "support_attributes"
        ] = ["DEBUG"]
        self.assertFalse(self._run(claims))

    def test_allows_debug_when_policy_permits(self):
        claims = _claims(dbgstat="enabled")
        claims["submods"]["confidential_space"][
            "support_attributes"
        ] = ["DEBUG"]
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc123testdigest"]
            ),
            allow_debug=True,
        )
        with mock.patch.dict(
            os.environ,
            {"ZTAB_TEST_ENVIRONMENT": "1"},
        ):
            self.assertTrue(
                self._run(claims, policy=policy)
            )

    # --- Memory monitoring ---

    def test_rejects_memory_monitoring(self):
        claims = _claims()
        claims["submods"]["confidential_space"][
            "monitoring_enabled"
        ]["memory"] = True
        self.assertFalse(self._run(claims))

    def test_allows_memory_monitoring_when_permitted(
        self,
    ):
        claims = _claims()
        claims["submods"]["confidential_space"][
            "monitoring_enabled"
        ]["memory"] = True
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc123testdigest"]
            ),
            allow_memory_monitoring=True,
        )
        with mock.patch.dict(
            os.environ,
            {"ZTAB_TEST_ENVIRONMENT": "1"},
        ):
            self.assertTrue(
                self._run(claims, policy=policy)
            )

    # --- Key binding ---

    def test_rejects_nonce_mismatch(self):
        nonce_mock = mock.patch.object(
            ita_verifier,
            "_compute_pubkey_hash_b64url",
            return_value="expected-nonce",
        )
        self.assertFalse(
            self._run(
                _claims(eat_nonce="wrong"),
                extra_patches=[nonce_mock],
            )
        )

    # --- Container identity ---

    def test_multiple_digests_accepted(self):
        policy = ItaPolicy(
            expected_image_digests=frozenset([
                "sha256:abc123testdigest",
                "sha256:other",
            ]),
        )
        self.assertTrue(
            self._run(_claims(), policy=policy)
        )

    def test_digest_mismatch(self):
        policy = ItaPolicy(
            expected_image_digests=frozenset([
                "sha256:aaa",
            ]),
        )
        self.assertFalse(
            self._run(_claims(), policy=policy)
        )

    def test_project_id_mismatch(self):
        claims = _claims()
        claims["submods"]["gce"] = {
            "project_id": "wrong"
        }
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc123testdigest"]
            ),
            expected_project_id="correct",
        )
        self.assertFalse(
            self._run(claims, policy=policy)
        )

    def test_project_id_empty_skipped(self):
        """Empty expected_project_id skips check."""
        self.assertTrue(self._run(_claims()))

    def test_service_account_mismatch(self):
        claims = _claims()
        claims["google_service_accounts"] = [
            "wrong@proj.iam.gserviceaccount.com"
        ]
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc123testdigest"]
            ),
            expected_service_account=(
                "correct@proj.iam.gserviceaccount.com"
            ),
        )
        self.assertFalse(
            self._run(claims, policy=policy)
        )

    def test_service_account_match(self):
        """Matching service account passes."""
        sa = "correct@proj.iam.gserviceaccount.com"
        claims = _claims()
        claims["google_service_accounts"] = [sa]
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc123testdigest"]
            ),
            expected_service_account=sa,
        )
        self.assertTrue(
            self._run(claims, policy=policy)
        )

    # --- swversion ---

    def test_swversion_below_minimum(self):
        claims = _claims()
        claims["swversion"] = ["200000"]
        self.assertFalse(self._run(claims))

    def test_swversion_missing_rejected(self):
        claims = _claims()
        claims["swversion"] = []
        self.assertFalse(self._run(claims))

    def test_swversion_below_custom_minimum(self):
        """Custom min_cs_version rejects lower version."""
        claims = _claims()
        claims["swversion"] = ["260500"]
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc123testdigest"]
            ),
            min_cs_version=300000,
        )
        self.assertFalse(
            self._run(claims, policy=policy)
        )


# ─── Policy Validation Tests ────────────────────────


class PolicyValidationTest(unittest.TestCase):

    def test_noop_blocked_outside_test_env(self):
        policy = NoopPolicy()
        with mock.patch.dict(
            os.environ, {}, clear=True
        ):
            with self.assertRaises(RuntimeError):
                policy.validate()

    def test_noop_allowed_in_test_env(self):
        policy = NoopPolicy()
        with mock.patch.dict(
            os.environ,
            {"ZTAB_TEST_ENVIRONMENT": "1"},
        ):
            policy.validate()  # no exception

    def test_ita_empty_digests_raises(self):
        policy = ItaPolicy(
            expected_image_digests=frozenset(),
        )
        with self.assertRaises(ValueError):
            policy.validate()

    def test_ita_zero_min_cs_version_raises(self):
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc"]
            ),
            min_cs_version=0,
        )
        with self.assertRaises(ValueError):
            policy.validate()

    def test_allow_debug_blocked_outside_test(self):
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc"]
            ),
            allow_debug=True,
        )
        with mock.patch.dict(
            os.environ, {}, clear=True
        ):
            with self.assertRaises(RuntimeError):
                policy.validate()

    def test_allow_debug_ok_in_test(self):
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc"]
            ),
            allow_debug=True,
        )
        with mock.patch.dict(
            os.environ,
            {"ZTAB_TEST_ENVIRONMENT": "1"},
        ):
            policy.validate()  # no exception

    def test_allow_memory_mon_blocked(self):
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc"]
            ),
            allow_memory_monitoring=True,
        )
        with mock.patch.dict(
            os.environ, {}, clear=True
        ):
            with self.assertRaises(RuntimeError):
                policy.validate()


# ─── Policy Serialization Tests ──────────────────────


class PolicySerializationTest(unittest.TestCase):

    def test_ita_roundtrip(self):
        policy = ItaPolicy(
            expected_image_digests=frozenset(
                ["sha256:abc", "sha256:def"]
            ),
            expected_project_id="my-proj",
            allow_debug=True,
            min_cs_version=270000,
        )
        d = policy.to_dict()
        restored = VerifierPolicy.from_dict(d)
        self.assertIsInstance(restored, ItaPolicy)
        self.assertEqual(
            restored.expected_image_digests,
            policy.expected_image_digests,
        )
        self.assertEqual(
            restored.expected_project_id, "my-proj"
        )
        self.assertTrue(restored.allow_debug)
        self.assertEqual(
            restored.min_cs_version, 270000
        )

    def test_noop_roundtrip(self):
        policy = NoopPolicy()
        d = policy.to_dict()
        restored = VerifierPolicy.from_dict(d)
        self.assertIsInstance(restored, NoopPolicy)

    def test_unknown_type_raises(self):
        with self.assertRaises(ValueError):
            VerifierPolicy.from_dict(
                {"verifier_type": "unknown"}
            )


# ─── Factory Tests ───────────────────────────────────


class VerifierFactoryTest(unittest.TestCase):

    def test_noop_without_test_env(self):
        policy = NoopPolicy()
        with mock.patch.dict(
            os.environ, {}, clear=True
        ):
            with self.assertRaises(RuntimeError):
                verifier_factory.get_verifier(policy)

    def test_noop_with_test_env(self):
        policy = NoopPolicy()
        _fake_noop = lambda token, cert: True
        with mock.patch.dict(
            os.environ,
            {"ZTAB_TEST_ENVIRONMENT": "1"},
        ):
            with mock.patch.dict(
                "sys.modules",
                {
                    "client": mock.MagicMock(
                        noop_verifier=_fake_noop
                    ),
                },
            ):
                v = verifier_factory.get_verifier(
                    policy
                )
                self.assertTrue(callable(v))
                self.assertTrue(
                    v("token", b"cert")
                )

    def test_ita_returns_callable(self):
        v = verifier_factory.get_verifier(
            _DEFAULT_POLICY
        )
        self.assertTrue(callable(v))


# ─── JWKS Caching Tests ─────────────────────────────


class JwksCachingTest(unittest.TestCase):

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
        ita_verifier,
        "_fetch_jwks",
        return_value={"keys": [{"kid": "k1"}]},
    )
    @mock.patch("agent.ita_verifier.time.time")
    def test_cache_hit_within_ttl(
        self, mock_time, mock_fetch, mock_nonce
    ):
        """Cached JWKS within TTL skips HTTP fetch."""
        now = 1000000.0
        cached_keys = {"keys": [{"kid": "k1"}]}

        ita_verifier._JWKS_CACHE["data"] = (
            cached_keys
        )
        ita_verifier._JWKS_CACHE["timestamp"] = (
            now - 100
        )
        mock_time.return_value = now

        patches = _patch_jwt_and_jwks(
            _claims(eat_nonce="expected-nonce")
        )
        patches = [
            p for p in patches
            if "_fetch_jwks" not in str(p)
        ]
        for p in patches:
            p.start()
        try:
            verifier = (
                ita_verifier.create_ita_verifier(
                    _DEFAULT_POLICY,
                )
            )
            verifier("tok", b"cert")
        finally:
            for p in patches:
                p.stop()

        mock_fetch.assert_not_called()

    @mock.patch.object(
        ita_verifier,
        "_compute_pubkey_hash_b64url",
        return_value="expected-nonce",
    )
    @mock.patch.object(
        ita_verifier,
        "_fetch_jwks",
        return_value={"keys": [{"kid": "k1"}]},
    )
    @mock.patch("agent.ita_verifier.time.time")
    def test_cache_miss_after_ttl(
        self, mock_time, mock_fetch, mock_nonce
    ):
        """Expired JWKS cache triggers HTTP fetch."""
        now = 1000000.0
        stale_keys = {"keys": [{"kid": "old"}]}

        ita_verifier._JWKS_CACHE["data"] = (
            stale_keys
        )
        ita_verifier._JWKS_CACHE["timestamp"] = (
            now - 86401
        )
        mock_time.return_value = now

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
                ita_verifier.create_ita_verifier(
                    _DEFAULT_POLICY,
                )
            )
            verifier("tok", b"cert")
        finally:
            for p in patches:
                p.stop()

        mock_fetch.assert_called()


class KeyRotationTest(unittest.TestCase):

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
            "kid": "k2",
            "alg": "RS256",
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
        mock_key1 = mock.MagicMock()
        mock_key1.key_id = "k1"
        mock_key1.key = "fake-key-1"
        keyset_no_k2 = mock.MagicMock()
        keyset_no_k2.keys = [mock_key1]

        mock_key2 = mock.MagicMock()
        mock_key2.key_id = "k2"
        mock_key2.key = "fake-key-2"
        keyset_with_k2 = mock.MagicMock()
        keyset_with_k2.keys = [
            mock_key1,
            mock_key2,
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
            ita_verifier.create_ita_verifier(
                _DEFAULT_POLICY,
            )
        )
        verifier("tok", b"cert")

        self.assertEqual(
            mock_fetch.call_count, 2
        )


class ContainerDigestTest(unittest.TestCase):

    def setUp(self):
        ita_verifier._JWKS_CACHE["data"] = None
        ita_verifier._JWKS_CACHE["timestamp"] = 0

    def tearDown(self):
        ita_verifier._JWKS_CACHE["data"] = None
        ita_verifier._JWKS_CACHE["timestamp"] = 0

    def test_missing_container_in_token(self):
        """Token with empty container claim fails."""
        claims_dict = _claims(
            eat_nonce="expected-nonce",
        )
        claims_dict["submods"]["container"] = {}
        nonce_patch = mock.patch.object(
            ita_verifier,
            "_compute_pubkey_hash_b64url",
            return_value="expected-nonce",
        )
        patches = _patch_jwt_and_jwks(claims_dict)
        patches.append(nonce_patch)

        verifier = (
            ita_verifier.create_ita_verifier(
                _DEFAULT_POLICY,
            )
        )
        for p in patches:
            p.start()
        try:
            self.assertFalse(
                verifier("tok", b"cert")
            )
        finally:
            for p in patches:
                p.stop()


if __name__ == "__main__":
    unittest.main()
