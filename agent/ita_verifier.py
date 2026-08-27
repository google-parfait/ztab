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
  2. Issuer claim (exact match: ITA only, GCA rejected)
  3. Confidential Computing claims:
     - hwmodel = INTEL_TDX (unconditional)
     - secboot = True (unconditional)
     - swname = CONFIDENTIAL_SPACE (unconditional)
     - cvm_compliance = gcp_compliant_cvm (unconditional)
     - dbgstat & support_attributes (allow_debug gated)
     - memory monitoring (allow_memory_monitoring gated)
     - swversion >= min_cs_version (unconditional)
  4. Key binding: eat_nonce matches SHA-256(server_pubkey)
  5. Container identity: image digest (required),
     project ID (optional), service account (optional)

Protocol-fixed constants are NOT parameterizable — they
are inherent to the ITA / Confidential Space protocol.
The only configurable aspects come from the ItaPolicy.
"""

import base64
import hashlib
import json
import logging
import threading
import time
from typing import Callable
from urllib import request as urllib_request

import jwt  # PyJWT
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    PublicFormat,
)

from verifier_policy import ItaPolicy

logger = logging.getLogger(__name__)

# ─── Protocol-fixed constants ────────────────────────
# These are inherent to ITA / Confidential Space and
# are NEVER parameterizable. Any legitimate CS TEE
# always reports these values.

ITA_JWKS_URL = (
    "https://portal.trustauthority.intel.com/certs"
)
ITA_ISSUER = (
    "https://portal.trustauthority.intel.com"
)
EXPECTED_HWMODEL = "INTEL_TDX"
EXPECTED_SWNAME = "CONFIDENTIAL_SPACE"
EXPECTED_CVM_COMPLIANCE = "gcp_compliant_cvm"
EXPECTED_AUDIENCE = "ztab_tls"

# Acceptable dbgstat values (dbgstat is always present
# on CS TEEs; we restrict which values we accept based
# on the allow_debug flag).
VALID_DBG_STATES = {
    "disabled-since-boot",
    "disabled",
    "enabled",
}

# Legacy constants kept for backward compatibility
# with any external references.
GCA_ISSUER = (
    "https://confidentialcomputing.googleapis.com"
)
DEFAULT_AUDIENCE = EXPECTED_AUDIENCE
VALID_HW_MODELS = {"GCP_INTEL_TDX", "INTEL_TDX"}


# ─── Helpers ─────────────────────────────────────────


def _base64url_decode(s: str) -> bytes:
    """Decode a Base64URL string (with or without padding)."""
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s)


def _base64url_encode_nopad(data: bytes) -> str:
    """Encode bytes to Base64URL without padding."""
    return (
        base64.urlsafe_b64encode(data)
        .rstrip(b"=")
        .decode("ascii")
    )


def _fetch_jwks(
    jwks_url: str, timeout: int = 10
) -> dict:
    """Fetch JWKS (JSON Web Key Set) from the given URL."""
    logger.info("Fetching JWKS from %s", jwks_url)
    req = urllib_request.Request(jwks_url)
    with urllib_request.urlopen(
        req, timeout=timeout
    ) as resp:
        jwks_data = json.loads(resp.read())
    logger.info(
        "Fetched %d keys from JWKS.",
        len(jwks_data.get("keys", [])),
    )
    return jwks_data


def _extract_pubkey_from_cert(
    cert_pem: bytes,
) -> bytes:
    """Extract raw SubjectPublicKeyInfo (DER) from PEM cert."""
    cert = x509.load_pem_x509_certificate(
        cert_pem, default_backend()
    )
    return cert.public_key().public_bytes(
        encoding=Encoding.DER,
        format=PublicFormat.SubjectPublicKeyInfo,
    )


def _compute_pubkey_hash_b64url(
    cert_pem: bytes,
) -> str:
    """Compute Base64URL(SHA-256(SPKI(cert))) — the expected nonce."""
    spki_der = _extract_pubkey_from_cert(cert_pem)
    digest = hashlib.sha256(spki_der).digest()
    return _base64url_encode_nopad(digest)


_JWKS_CACHE = {"data": None, "timestamp": 0}
_JWKS_LOCK = threading.Lock()
_JWKS_TTL = 86400  # 24 hours


# ─── Modular enforcement methods ─────────────────────


def _verify_issuer(claims: dict) -> bool:
    """Verify iss == ITA_ISSUER (unconditional)."""
    iss = claims.get("iss", "")
    if iss != ITA_ISSUER:
        logger.error(
            "Issuer mismatch: got '%s', "
            "expected '%s'",
            iss,
            ITA_ISSUER,
        )
        return False
    logger.info("  [OK] Issuer: %s", iss)
    return True


def _verify_hw_claims(
    claims: dict, policy: ItaPolicy
) -> bool:
    """Verify hwmodel, secboot, dbgstat, support_attributes.

    hwmodel and secboot are unconditional. dbgstat and
    support_attributes respect policy.allow_debug.
    """
    # hwmodel — unconditional exact match.
    hwmodel = claims.get("hwmodel", "")
    if hwmodel != EXPECTED_HWMODEL:
        logger.error(
            "hwmodel mismatch: got '%s', "
            "expected '%s'",
            hwmodel,
            EXPECTED_HWMODEL,
        )
        return False
    logger.info("  [OK] HW model: %s", hwmodel)

    # secboot — unconditional.
    secboot = claims.get("secboot", False)
    if secboot is not True:
        logger.error(
            "Secure boot not enabled (secboot=%s)",
            secboot,
        )
        return False
    logger.info("  [OK] Secure boot: enabled")

    # dbgstat — gated by allow_debug.
    dbgstat = claims.get("dbgstat", "")
    if not policy.allow_debug:
        if dbgstat not in {
            "disabled",
            "disabled-since-boot",
        }:
            logger.error(
                "Debug not disabled (dbgstat='%s')",
                dbgstat,
            )
            return False
    elif dbgstat not in VALID_DBG_STATES:
        logger.error("Invalid dbgstat: '%s'", dbgstat)
        return False
    logger.info("  [OK] Debug status: %s", dbgstat)

    # support_attributes — reject DEBUG unless allowed.
    if not policy.allow_debug:
        cs = claims.get("submods", {}).get(
            "confidential_space", {}
        )
        attrs = cs.get("support_attributes", [])
        if "DEBUG" in attrs:
            logger.error(
                "support_attributes contains DEBUG"
            )
            return False
        logger.info(
            "  [OK] support_attributes: no DEBUG"
        )

    return True


def _verify_platform_identity(
    claims: dict, policy: ItaPolicy
) -> bool:
    """Verify swname, cvm_compliance, memory monitoring, swversion."""
    
    # swname is at the root of the claims.
    swname = claims.get("swname", "")
    if swname != EXPECTED_SWNAME:
        logger.error(
            "swname mismatch: got '%s', "
            "expected '%s'",
            swname,
            EXPECTED_SWNAME,
        )
        return False
    logger.info("  [OK] swname: %s", swname)

    # cvm_compliance_status is under tdx.
    tdx = claims.get("tdx", {})
    cvm_status = tdx.get(
        "cvm_compliance_status", ""
    )
    if cvm_status != EXPECTED_CVM_COMPLIANCE:
        logger.error(
            "cvm_compliance mismatch: got '%s', "
            "expected '%s'",
            cvm_status,
            EXPECTED_CVM_COMPLIANCE,
        )
        return False
    logger.info(
        "  [OK] cvm_compliance: %s", cvm_status
    )

    # Memory monitoring is under submods.confidential_space.
    cs = claims.get("submods", {}).get(
        "confidential_space", {}
    )
    if not policy.allow_memory_monitoring:
        mon = cs.get("monitoring_enabled", {})
        if mon.get("memory") is True:
            logger.error("Memory monitoring enabled")
            return False
        logger.info(
            "  [OK] Memory monitoring: disabled"
        )

    # swversion is at the root, and is a list of strings.
    swversion_list = claims.get("swversion", [])
    if not isinstance(swversion_list, list):
        # Fallback if it happens to be a single string for some reason
        swversion_list = [swversion_list]

    if policy.min_cs_version:
        if not swversion_list:
            logger.error("swversion claim missing or empty, but min_cs_version required")
            return False
            
        meets_minimum = False
        for v in swversion_list:
            try:
                ver_int = int(str(v))
                if ver_int >= policy.min_cs_version:
                    meets_minimum = True
                    break
            except (ValueError, TypeError):
                continue
                
        if not meets_minimum:
            logger.error(
                "swversion %s below minimum %d",
                swversion_list,
                policy.min_cs_version,
            )
            return False
        logger.info(
            "  [OK] swversion %s meets minimum %d", 
            swversion_list, policy.min_cs_version
        )
    else:
        logger.info("  [INFO] swversion check skipped by policy.")

    return True



def _verify_key_binding(
    claims: dict, cert_pem: bytes
) -> bool:
    """Verify eat_nonce matches SHA-256(SPKI(cert))."""
    eat_nonce = claims.get("eat_nonce", "")
    if not eat_nonce:
        logger.error("JWT missing 'eat_nonce' claim.")
        return False

    # Handle eat_nonce as either string or list.
    if isinstance(eat_nonce, list):
        nonce_value = (
            eat_nonce[0] if eat_nonce else ""
        )
    else:
        nonce_value = eat_nonce

    expected_nonce = _compute_pubkey_hash_b64url(
        cert_pem
    )
    if nonce_value != expected_nonce:
        logger.error("eat_nonce mismatch!")
        logger.error(
            "  Token nonce:    %s", nonce_value
        )
        logger.error(
            "  Expected nonce: %s", expected_nonce
        )
        logger.error(
            "  This means the attestation token is "
            "not bound to this server's TLS key."
        )
        return False
    logger.info(
        "  [OK] Key binding "
        "(eat_nonce matches cert pubkey hash)"
    )
    return True


def _verify_container_identity(
    claims: dict, policy: ItaPolicy
) -> bool:
    """Verify container image digest, project ID, service account."""
    submods = claims.get("submods", {})
    container = submods.get("container", {})

    # Image digest (mandatory).
    actual_digest = container.get(
        "image_digest", ""
    )
    if not actual_digest:
        logger.error(
            "Attestation token does not contain a "
            "container image digest."
        )
        return False
    if (
        actual_digest
        not in policy.expected_image_digests
    ):
        logger.error("Image digest mismatch!")
        logger.error(
            "  Actual:   %s", actual_digest
        )
        logger.error(
            "  Expected: %s",
            sorted(policy.expected_image_digests),
        )
        return False
    logger.info(
        "  [OK] Container image digest: %s",
        actual_digest,
    )

    # Project ID (optional — skip if empty).
    if policy.expected_project_id:
        gce = submods.get("gce", {})
        pid = gce.get("project_id", "")
        if pid != policy.expected_project_id:
            logger.error(
                "project_id mismatch: '%s' vs '%s'",
                pid,
                policy.expected_project_id,
            )
            return False
        logger.info("  [OK] project_id: %s", pid)

    # Service account (optional — skip if empty).
    if policy.expected_service_account:
        accounts = claims.get(
            "google_service_accounts", []
        )
        if (
            policy.expected_service_account
            not in accounts
        ):
            logger.error(
                "service_account '%s' not in %s",
                policy.expected_service_account,
                accounts,
            )
            return False
        logger.info(
            "  [OK] service_account: %s",
            policy.expected_service_account,
        )

    return True


# ─── Main verifier factory ───────────────────────────


def create_ita_verifier(
    policy: ItaPolicy,
) -> Callable[[str, bytes], bool]:
    """Create an ITA attestation verifier function.

    Returns a verifier compatible with ZtabChannel's
    AttestationVerifier type:
        (token: str, cert_pem: bytes) -> bool

    Args:
        policy: An ItaPolicy containing all
            configurable verification parameters.
            The policy must have been validated
            before calling this function.
    """
    jwks_url = ITA_JWKS_URL

    def get_jwks() -> dict:
        now = time.time()
        # Fast path without lock
        if (
            _JWKS_CACHE["data"] is not None
            and (now - _JWKS_CACHE["timestamp"])
            <= _JWKS_TTL
        ):
            return _JWKS_CACHE["data"]

        with _JWKS_LOCK:
            # Double-check inside lock
            if (
                _JWKS_CACHE["data"] is None
                or (now - _JWKS_CACHE["timestamp"])
                > _JWKS_TTL
            ):
                logger.info(
                    "Fetching updated JWKS from %s",
                    jwks_url,
                )
                _JWKS_CACHE["data"] = _fetch_jwks(
                    jwks_url
                )
                _JWKS_CACHE["timestamp"] = now
            return _JWKS_CACHE["data"]

    def ita_verifier(
        token: str, cert_pem: bytes
    ) -> bool:
        """Verify an ITA attestation token."""

        logger.info("=" * 60)
        logger.info("ITA ATTESTATION VERIFICATION")
        logger.info("-" * 60)

        # --- Step 1: Verify JWT signature ---
        try:
            jwks_data = get_jwks()
            signing_keys = jwt.PyJWKSet.from_dict(
                jwks_data
            )

            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if not kid:
                logger.error(
                    "JWT has no 'kid' header."
                )
                return False

            matching_key = None
            for key in signing_keys.keys:
                if key.key_id == kid:
                    matching_key = key
                    break

            if matching_key is None:
                logger.info(
                    "Key kid='%s' not found, "
                    "forcing JWKS refresh.",
                    kid,
                )
                with _JWKS_LOCK:
                    _JWKS_CACHE["timestamp"] = 0
                jwks_data = get_jwks()
                signing_keys = (
                    jwt.PyJWKSet.from_dict(jwks_data)
                )
                for key in signing_keys.keys:
                    if key.key_id == kid:
                        matching_key = key
                        break

            if matching_key is None:
                logger.error(
                    "No key with kid='%s' found "
                    "in JWKS after refresh.",
                    kid,
                )
                return False

            claims = jwt.decode(
                token,
                matching_key.key,
                algorithms=[
                    "RS256",
                    "RS384",
                    "RS512",
                    "ES256",
                    "ES384",
                ],
                audience=EXPECTED_AUDIENCE,
                options={
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_iss": False,
                    "verify_aud": True,
                },
                leeway=policy.clock_skew_seconds,
            )

            import os
            import json
            if os.environ.get("ZTAB_TEST_ENVIRONMENT") == "1":
                logger.info("  [VERBOSE] Token claims:")
                logger.info(json.dumps(claims, indent=2))

            logger.info(
                "  [OK] JWT signature verified "
                "(kid=%s)",
                kid,
            )

        except jwt.exceptions.ExpiredSignatureError:
            logger.error("JWT has expired.")
            return False
        except jwt.exceptions.InvalidAudienceError:
            logger.error(
                "JWT audience mismatch "
                "(expected '%s').",
                EXPECTED_AUDIENCE,
            )
            return False
        except jwt.exceptions.DecodeError as e:
            logger.error(
                "JWT decode/signature failure: %s",
                e,
            )
            return False
        except Exception as e:
            logger.error(
                "JWT verification failed: %s", e
            )
            return False

        # --- Step 2: Verify issuer (unconditional) ---
        if not _verify_issuer(claims):
            return False

        # --- Step 3: Verify HW claims ---
        if not _verify_hw_claims(claims, policy):
            return False

        # --- Step 4: Verify platform identity ---
        if not _verify_platform_identity(
            claims, policy
        ):
            return False

        # --- Step 5: Key binding (eat_nonce) ---
        if not _verify_key_binding(
            claims, cert_pem
        ):
            return False

        # --- Step 6: Container identity ---
        if not _verify_container_identity(
            claims, policy
        ):
            return False

        logger.info("-" * 60)
        logger.info("ATTESTATION VERIFICATION PASSED")
        logger.info("=" * 60)
        return True

    return ita_verifier
