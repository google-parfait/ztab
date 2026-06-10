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

"""Attestation utility functions."""

from typing import Optional
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import ObjectIdentifier

# Must match the OID used by the C++ server in tls_cert_generator.cc.
ATTESTATION_OID = ObjectIdentifier("1.3.6.1.4.1.99999.1")

def extract_attestation_token(cert_pem: bytes) -> Optional[str]:
    """Extracts the attestation token from a PEM certificate's custom extension.

    Returns the token as a UTF-8 string, or None if the extension is not found.
    """
    cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
    try:
        ext = cert.extensions.get_extension_for_oid(ATTESTATION_OID)
    except x509.ExtensionNotFound:
        return None

    # The extension value is an UnrecognizedExtension wrapping raw bytes.
    # The server encodes the token as: ASN1_OCTET_STRING(token_bytes).
    # The cryptography library gives us the outer DER, so we need to strip
    # the ASN.1 OCTET STRING tag+length header.
    raw = ext.value.value
    if len(raw) < 2:
        return None

    # ASN.1 OCTET STRING: tag=0x04, then length encoding.
    if raw[0] != 0x04:
        # Not wrapped in OCTET STRING; try raw.
        return raw.decode("utf-8", errors="replace")

    # Simple length form: next byte is the length.
    if raw[1] < 0x80:
        payload = raw[2:]
    else:
        # Long form: lower 7 bits of raw[1] give the number of length bytes.
        num_length_bytes = raw[1] & 0x7F
        payload = raw[2 + num_length_bytes:]

    return payload.decode("utf-8", errors="replace")
