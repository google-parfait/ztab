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

"""Unit tests for attestation token extraction."""

import datetime
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import (
    ec,
)
from cryptography.x509.oid import NameOID

from agent.attestation import (
    ATTESTATION_OID,
    extract_attestation_token,
)


def _build_self_signed_cert(extensions=None):
  """Build a minimal self-signed PEM certificate.

  Args:
    extensions: optional list of (extension, critical)
                tuples to add.

  Returns:
    PEM-encoded certificate bytes.
  """
  key = ec.generate_private_key(ec.SECP256R1())
  subject = x509.Name([
      x509.NameAttribute(NameOID.COMMON_NAME, "test"),
  ])
  now = datetime.datetime.now(datetime.timezone.utc)
  builder = (
      x509.CertificateBuilder()
      .subject_name(subject)
      .issuer_name(subject)
      .public_key(key.public_key())
      .serial_number(x509.random_serial_number())
      .not_valid_before(now)
      .not_valid_after(
          now + datetime.timedelta(days=1)
      )
  )
  for ext, critical in (extensions or []):
    builder = builder.add_extension(
        ext, critical=critical
    )
  cert = builder.sign(key, hashes.SHA256())
  return cert.public_bytes(
      encoding=(
          __import__("cryptography.hazmat.primitives"
                     ".serialization",
                     fromlist=["Encoding"])
          .Encoding.PEM
      )
  )


class ExtractAttestationTokenTest(unittest.TestCase):
  """Tests for extract_attestation_token."""

  def test_extract_token_from_cert_with_oid(self):
    """Cert with custom OID yields the embedded token."""
    token_str = "test-token-123"
    # DER OCTET STRING: tag 0x04, length 14,
    # then the UTF-8 payload.
    der_value = b"\x04\x0e" + token_str.encode()
    ext = x509.UnrecognizedExtension(
        ATTESTATION_OID, der_value
    )
    pem = _build_self_signed_cert(
        extensions=[(ext, False)]
    )
    result = extract_attestation_token(pem)
    self.assertEqual(result, token_str)

  def test_extract_token_missing_oid(self):
    """Cert without the attestation OID returns None."""
    pem = _build_self_signed_cert()
    result = extract_attestation_token(pem)
    self.assertIsNone(result)


if __name__ == "__main__":
  unittest.main()
