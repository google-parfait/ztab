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

"""ZTAB Client Library.

Connects to a ZTAB server over TLS, extracts the attestation token from the
server's certificate, and provides a way to call RPCs.

For initial testing the verifier is a no-op that prints the token. In
production, the verifier will validate the GCP OIDC JWT signature, claims, and
the key-binding nonce.
"""

import ssl
import socket
from typing import Callable, Optional

from cryptography import x509
from cryptography.hazmat.backends import default_backend
import grpc

from attestation import extract_attestation_token, ATTESTATION_OID


def _fetch_server_cert_pem(host: str, port: int) -> bytes:
    """Connects via raw TLS and retrieves the server certificate in PEM format.

    We need this because gRPC Python doesn't expose the peer certificate
    directly. We make a short-lived TLS connection to grab the cert, then use
    it for both attestation extraction and as the trusted root for the gRPC
    channel (since the cert is self-signed and ephemeral).
    """
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # Accept the self-signed cert. We verify it via attestation, not CA chain.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=10) as sock:
        with context.wrap_socket(sock, server_hostname="ztab-tee") as tls_sock:
            der_cert = tls_sock.getpeercert(binary_form=True)
            if der_cert is None:
                raise RuntimeError("Server did not present a certificate.")
            # Convert DER to PEM via cryptography.
            cert = x509.load_der_x509_certificate(der_cert, default_backend())
            from cryptography.hazmat.primitives.serialization import Encoding
            return cert.public_bytes(Encoding.PEM)





# Type alias for a verifier function. Takes the attestation token string
# and the PEM cert bytes. Returns True if verification passes.
AttestationVerifier = Callable[[str, bytes], bool]


def noop_verifier(token: str, cert_pem: bytes) -> bool:
    """No-op verifier that prints the attestation token and always passes.
    
    WARNING: This verifier performs no actual validation and should ONLY be used
    for testing or in environments where attestation is not required.
    """
    print("=" * 60)
    print("ATTESTATION TOKEN (no-op verifier, not validated):")
    print("-" * 60)
    print(token)
    print("=" * 60)
    return True


class ZtabChannel:
    """A gRPC channel to a ZTAB server with attestation verification.

    Usage:
        channel = ZtabChannel("localhost", 8000)
        channel.connect()
        # Use channel.grpc_channel for RPC stubs.
    """

    def __init__(
        self,
        host: str,
        port: int,
        verifier: AttestationVerifier = noop_verifier,
    ):
        self.host = host
        self.port = port
        self.verifier = verifier
        self.grpc_channel: Optional[grpc.Channel] = None
        self.attestation_token: Optional[str] = None
        self._cert_pem: Optional[bytes] = None

    def connect(self) -> grpc.Channel:
        """Connects to the server, verifies attestation, returns gRPC channel.

        Raises RuntimeError if attestation verification fails.
        """
        target = f"{self.host}:{self.port}"

        # Step 1: Fetch the server's self-signed certificate.
        print(f"Fetching server certificate from {target}...")
        self._cert_pem = _fetch_server_cert_pem(self.host, self.port)
        print(f"Certificate received ({len(self._cert_pem)} bytes PEM).")

        # Step 2: Extract the attestation token.
        self.attestation_token = extract_attestation_token(self._cert_pem)
        if self.attestation_token is None:
            raise RuntimeError(
                "Server certificate does not contain an attestation extension "
                f"(OID {ATTESTATION_OID.dotted_string})."
            )

        # Step 3: Run the verifier.
        if not self.verifier(self.attestation_token, self._cert_pem):
            raise RuntimeError("Attestation verification failed.")
        print("Attestation verified (or accepted by no-op verifier).")

        # Step 4: Create a gRPC channel trusting this specific cert.
        creds = grpc.ssl_channel_credentials(
            root_certificates=self._cert_pem
        )
        options = (('grpc.ssl_target_name_override', 'ztab-tee'),)
        self.grpc_channel = grpc.secure_channel(target, creds, options=options)
        print(f"gRPC channel established to {target} (override target to 'ztab-tee').")

        return self.grpc_channel

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def close(self):
        if self.grpc_channel is not None:
            self.grpc_channel.close()
            self.grpc_channel = None
