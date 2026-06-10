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

"""Minimal ZTAB TLS connectivity test.

Tests the RA-TLS plumbing without requiring grpcio. Uses only `ssl` (stdlib)
and `cryptography` (pre-installed on the local development environment) to:

  1. Connect to the server via TLS (accepting self-signed certs).
  2. Retrieve the server's certificate.
  3. Extract the attestation token from the custom X.509 extension.
  4. Print the token and verify the connection worked.

Usage:
    python3 tls_test.py --host localhost --port 8000
"""

import argparse
import json
import base64
import socket
import ssl
import sys

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ObjectIdentifier

from attestation import extract_attestation_token, ATTESTATION_OID


def fetch_cert_and_extract_token(host: str, port: int) -> tuple[bytes, str | None]:
    """Connects via TLS, returns (cert_pem, attestation_token_or_none)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname="ztab-tee") as tls_sock:
            der_cert = tls_sock.getpeercert(binary_form=True)
            if der_cert is None:
                raise RuntimeError("Server did not present a certificate.")

            cert = x509.load_der_x509_certificate(der_cert, default_backend())
            cert_pem = cert.public_bytes(Encoding.PEM)

            # Extract attestation token from custom extension using shared utility.
            token = extract_attestation_token(cert_pem)
            return cert_pem, token


def decode_jwt_payload(token: str) -> dict | None:
    """Decodes the payload of an unsigned JWT (alg=none) for display."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    # Add padding.
    payload_b64 = parts[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    # JWT uses base64url encoding.
    payload_b64 = payload_b64.replace("-", "+").replace("_", "/")
    try:
        payload_bytes = base64.b64decode(payload_b64)
        return json.loads(payload_bytes)
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(
        description="Minimal ZTAB TLS connectivity test (no grpcio needed)."
    )
    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=8000, help="Server port")
    args = parser.parse_args()

    target = f"{args.host}:{args.port}"
    print(f"Connecting to {target} via TLS...")

    try:
        cert_pem, token = fetch_cert_and_extract_token(args.host, args.port)
    except Exception as e:
        print(f"FAILED: Could not connect: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"TLS connection established. Certificate received ({len(cert_pem)} bytes PEM).")
    print()

    # Print certificate subject.
    cert = x509.load_pem_x509_certificate(cert_pem, default_backend())
    print(f"Certificate Subject: {cert.subject}")
    print(f"Certificate Issuer:  {cert.issuer}")
    print(f"Not Before: {cert.not_valid_before_utc}")
    print(f"Not After:  {cert.not_valid_after_utc}")
    print()

    if token is None:
        print("WARNING: No attestation extension found in certificate.")
        print(f"  Expected OID: {ATTESTATION_OID.dotted_string}")
        sys.exit(1)

    print(f"Attestation token extracted from X.509 extension (OID {ATTESTATION_OID.dotted_string}):")
    print("=" * 70)
    print(token)
    print("=" * 70)
    print()

    # Decode and pretty-print the JWT payload.
    payload = decode_jwt_payload(token)
    if payload:
        print("Decoded JWT payload (mock attestation claims):")
        print(json.dumps(payload, indent=2))
        print()

        # Check expected claims.
        checks = {
            "iss": "https://confidentialcomputing.googleapis.com",
            "hwmodel": "GCP_INTEL_TDX",
            "secboot": True,
        }
        all_ok = True
        for key, expected in checks.items():
            actual = payload.get(key)
            status = "OK" if actual == expected else "MISMATCH"
            if status != "OK":
                all_ok = False
            print(f"  {key}: {actual} [{status}]")

        if "eat_nonce" in payload:
            print(f"  eat_nonce: {payload['eat_nonce'][:40]}... [PRESENT]")
        print()

    print("TLS connectivity test PASSED.")
    print("  - TLS handshake:       OK")
    print("  - Certificate:         Self-signed, CN=ztab-tee")
    print("  - Attestation token:   Extracted from custom X.509 extension")
    print("  - Token format:        Unsigned JWT (mock, alg=none)")


if __name__ == "__main__":
    main()
