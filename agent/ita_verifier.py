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

"""ITA (Intel Trust Authority) attestation token verifier for ZTAB.

Verifies JWT attestation tokens issued by the GCP Confidential Space agent
via Intel Trust Authority. The verification includes:

  1. JWT signature verification using ITA's JWKS public keys
  2. Standard JWT claims (iss, aud, exp, nbf)
  3. Confidential Computing claims (hwmodel, secboot, dbgstat)
  4. Key binding: eat_nonce matches SHA-256(server_pubkey)
  5. Optional: container image digest check

Usage as a pluggable verifier for ZtabChannel:

    from ita_verifier import create_ita_verifier

    verifier = create_ita_verifier()
    channel = ZtabChannel("10.0.0.1", 8000, verifier=verifier)
    channel.connect()
"""

import base64
import hashlib
import json
import logging
import threading
import time
from typing import Callable, Optional, Set
from urllib import request as urllib_request

import jwt  # PyJWT
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

logger = logging.getLogger(__name__)

# ITA JWKS endpoint (same as used by CFC containers/gcp).
ITA_JWKS_URL = "https://portal.trustauthority.intel.com/certs"

# Expected issuers per attestation provider.
ITA_ISSUER = "https://portal.trustauthority.intel.com"
GCA_ISSUER = "https://confidentialcomputing.googleapis.com"

# Default expected audience (must match the server's attestation request).
DEFAULT_AUDIENCE = "ztab_tls"

# Acceptable hwmodel values for Intel TDX.
VALID_HW_MODELS = {"GCP_INTEL_TDX", "INTEL_TDX"}

# Acceptable dbgstat values.
# Note: require_debug_disabled is True by default for production security,
# so 'enabled' will be rejected unless explicitly overridden by the caller
# (e.g., using --allow-debug-tee for local development/preview images).
VALID_DBG_STATES = {"disabled-since-boot", "disabled", "enabled"}


def _base64url_decode(s: str) -> bytes:
    """Decode a Base64URL string (with or without padding)."""
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s)


def _base64url_encode_nopad(data: bytes) -> str:
    """Encode bytes to Base64URL without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _fetch_jwks(jwks_url: str, timeout: int = 10) -> dict:
    """Fetch JWKS (JSON Web Key Set) from the given URL."""
    logger.info("Fetching JWKS from %s", jwks_url)
    req = urllib_request.Request(jwks_url)
    with urllib_request.urlopen(req, timeout=timeout) as resp:
        jwks_data = json.loads(resp.read())
    logger.info("Fetched %d keys from JWKS.", len(jwks_data.get("keys", [])))
    return jwks_data


def _extract_pubkey_from_cert(cert_pem: bytes) -> bytes:
    """Extract the raw SubjectPublicKeyInfo (DER) from a PEM certificate."""
    cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
    return cert.public_key().public_bytes(
        encoding=Encoding.DER,
        format=PublicFormat.SubjectPublicKeyInfo,
    )


def _compute_pubkey_hash_b64url(cert_pem: bytes) -> str:
    """Compute Base64URL(SHA-256(SubjectPublicKeyInfo(cert))) — the expected nonce."""
    spki_der = _extract_pubkey_from_cert(cert_pem)
    digest = hashlib.sha256(spki_der).digest()
    return _base64url_encode_nopad(digest)

_JWKS_CACHE = {"data": None, "timestamp": 0}
_JWKS_LOCK = threading.Lock()
_JWKS_TTL = 86400  # 24 hours

def create_ita_verifier(
    expected_image_digest: str,
    jwks_url: str = ITA_JWKS_URL,
    expected_audience: str = DEFAULT_AUDIENCE,
    expected_issuers: Optional[Set[str]] = None,
    require_secure_boot: bool = True,
    require_debug_disabled: bool = True,
    clock_skew_seconds: int = 300,
) -> Callable[[str, bytes], bool]:
    """Create an ITA attestation verifier function.

    Returns a verifier compatible with ZtabChannel's AttestationVerifier type:
        (token: str, cert_pem: bytes) -> bool

    Args:
        expected_image_digest: Expected container image digest (e.g. 'sha256:...').
            Mandatory for establishing trust against the correct server workload.
        jwks_url: URL to fetch ITA JWKS from.
        expected_audience: Expected 'aud' claim.
        expected_issuers: Set of acceptable 'iss' values. Defaults to ITA issuer.
        require_secure_boot: If True, require secboot == True.
        require_debug_disabled: If True, validate dbgstat against VALID_DBG_STATES.
            Note: This defaults to True for strict security. If testing with GCP Confidential
            Space preview images, you must explicitly pass --allow-debug-tee to accept
            the 'enabled' debug state.
        clock_skew_seconds: Allowed clock skew for exp/nbf validation.
    """
    if not expected_image_digest:
        raise ValueError(
            "expected_image_digest must be a non-empty string (e.g. 'sha256:...'). "
            "Without container image digest verification, any Confidential Space workload can produce a valid token."
        )

    if expected_issuers is None:
        expected_issuers = {ITA_ISSUER}

    def get_jwks() -> dict:
        now = time.time()
        # Fast path without lock
        if _JWKS_CACHE["data"] is not None and (now - _JWKS_CACHE["timestamp"]) <= _JWKS_TTL:
            return _JWKS_CACHE["data"]
            
        with _JWKS_LOCK:
            # Double-check inside lock
            if _JWKS_CACHE["data"] is None or (now - _JWKS_CACHE["timestamp"]) > _JWKS_TTL:
                logger.info("Fetching updated JWKS from %s", jwks_url)
                _JWKS_CACHE["data"] = _fetch_jwks(jwks_url)
                _JWKS_CACHE["timestamp"] = now
            return _JWKS_CACHE["data"]

    def ita_verifier(token: str, cert_pem: bytes) -> bool:
        """Verify an ITA attestation token against the server's certificate."""

        logger.info("=" * 60)
        logger.info("ITA ATTESTATION VERIFICATION")
        logger.info("-" * 60)

        # --- Step 1: Verify JWT signature ---
        try:
            # PyJWT handles JWKS-based verification: it finds the matching
            # key by 'kid' header and verifies the signature.
            jwks_data = get_jwks()
            signing_keys = jwt.PyJWKSet.from_dict(jwks_data)

            # Decode the JWT header to find the kid.
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if not kid:
                logger.error("JWT has no 'kid' header.")
                return False

            # Find the matching key.
            matching_key = None
            for key in signing_keys.keys:
                if key.key_id == kid:
                    matching_key = key
                    break

            if matching_key is None:
                logger.info("Key kid='%s' not found, forcing JWKS refresh.", kid)
                with _JWKS_LOCK:
                    _JWKS_CACHE["timestamp"] = 0
                jwks_data = get_jwks()
                signing_keys = jwt.PyJWKSet.from_dict(jwks_data)
                for key in signing_keys.keys:
                    if key.key_id == kid:
                        matching_key = key
                        break

            if matching_key is None:
                logger.error("No key with kid='%s' found in JWKS after refresh.", kid)
                return False

            # Verify signature and decode claims.
            claims = jwt.decode(
                token,
                matching_key.key,
                algorithms=["RS256", "RS384", "RS512", "ES256", "ES384"],
                audience=expected_audience,
                options={
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iss": False,  # We check iss manually below.
                    "verify_aud": True,
                },
                leeway=clock_skew_seconds,
            )
            logger.info("  [OK] JWT signature verified (kid=%s)", kid)

        except jwt.exceptions.ExpiredSignatureError:
            logger.error("JWT has expired.")
            return False
        except jwt.exceptions.InvalidAudienceError:
            logger.error("JWT audience mismatch (expected '%s').",
                         expected_audience)
            return False
        except jwt.exceptions.DecodeError as e:
            logger.error("JWT decode/signature failure: %s", e)
            return False
        except Exception as e:
            logger.error("JWT verification failed: %s", e)
            return False

        # --- Step 2: Verify issuer ---
        iss = claims.get("iss", "")
        if iss not in expected_issuers:
            logger.error("Unexpected issuer '%s' (expected one of %s)",
                         iss, expected_issuers)
            return False
        logger.info("  [OK] Issuer: %s", iss)

        # --- Step 3: Verify Confidential Computing claims ---
        # hwmodel
        hwmodel = claims.get("hwmodel", "")
        if hwmodel not in VALID_HW_MODELS:
            logger.error("Unexpected hwmodel '%s' (expected one of %s)",
                         hwmodel, VALID_HW_MODELS)
            return False
        logger.info("  [OK] HW model: %s", hwmodel)

        # secboot
        if require_secure_boot:
            secboot = claims.get("secboot", False)
            if secboot is not True:
                logger.error("Secure boot not enabled (secboot=%s)", secboot)
                return False
            logger.info("  [OK] Secure boot: enabled")

        # dbgstat
        dbgstat = claims.get("dbgstat", "")
        if require_debug_disabled and dbgstat not in {"disabled", "disabled-since-boot"}:
            logger.error("Debug not disabled (dbgstat='%s'). require_debug_disabled=True.", dbgstat)
            return False
        elif dbgstat not in VALID_DBG_STATES:
            logger.error("Invalid dbgstat: '%s'", dbgstat)
            return False
        logger.info("  [OK] Debug status: %s", dbgstat)

        # --- Step 4: Key binding (eat_nonce) ---
        eat_nonce = claims.get("eat_nonce", "")
        if not eat_nonce:
            logger.error("JWT missing 'eat_nonce' claim.")
            return False

        # Handle eat_nonce as either a string or a list of strings.
        if isinstance(eat_nonce, list):
            nonce_value = eat_nonce[0] if eat_nonce else ""
        else:
            nonce_value = eat_nonce

        expected_nonce = _compute_pubkey_hash_b64url(cert_pem)
        if nonce_value != expected_nonce:
            logger.error("eat_nonce mismatch!")
            logger.error("  Token nonce:    %s", nonce_value)
            logger.error("  Expected nonce: %s", expected_nonce)
            logger.error("  This means the attestation token is not bound to "
                         "this server's TLS key.")
            return False
        logger.info("  [OK] Key binding (eat_nonce matches cert pubkey hash)")

        # --- Step 5: Container image digest ---
        submods = claims.get("submods", {})
        container = submods.get("container", {})
        actual_digest = container.get("image_digest", "")
        if not actual_digest:
            logger.error(
                "Attestation token does not contain a container image digest."
            )
            return False
        if actual_digest != expected_image_digest:
            logger.error("Image digest mismatch!")
            logger.error("  Actual:   %s", actual_digest)
            logger.error("  Expected: %s", expected_image_digest)
            return False
        logger.info(
            "  [OK] Container image digest: %s",
            actual_digest,
        )

        logger.info("-" * 60)
        logger.info("ATTESTATION VERIFICATION PASSED")
        logger.info("=" * 60)
        return True

    return ita_verifier
