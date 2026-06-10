# ZTAB TEE Server

C++ gRPC server intended for deployment inside a
[Confidential Space][cs] Trusted Execution Environment (TEE).

[cs]: https://cloud.google.com/confidential-computing/confidential-space/docs/confidential-space-overview

## What This Server Does

The server is the trusted broker component of ZTAB. It runs
inside hardware-isolated memory (e.g., Intel TDX or AMD SEV-SNP
via GCP Confidential Space), ensuring that:

*   **No one can observe session data** — not the cloud
    provider, not the host hypervisor, not other tenants.
*   **Clients can verify the server's integrity** before
    sending data, via Remote Attestation (RA-TLS).

### RA-TLS (Remote Attestation TLS)

On startup, the server:

1.  Generates an **ephemeral NIST P-256 key pair** (via
    BoringSSL).
2.  Computes the **SHA-256 hash of the public key**.
3.  Fetches (or, in dev mode, generates a mock) **attestation
    token** — an unsigned JWT whose `eat_nonce` claim contains
    the public key hash. In production, this token is obtained
    from the GCP Confidential Computing metadata endpoint and
    is signed by Google's attestation service.
4.  Embeds the attestation token into a **custom X.509
    extension** (OID `1.3.6.1.4.1.99999.1`) on a self-signed
    TLS certificate.
5.  Starts the gRPC server using these credentials.

Clients extract the attestation token from the TLS certificate
during the handshake, verify the JWT signature and claims, and
confirm that the `eat_nonce` matches the server's public key —
proving the key was generated inside a genuine TEE.

## Source Files

| File | Description |
| :--- | :--- |
| `main.cc` | Server entry point. Key generation, mock attestation, certificate creation, gRPC startup. Contains `AgentBrokerService` (currently `Echo`). |
| `tls_cert_generator.h/.cc` | `EphemeralCredentialGenerator` class. EC key generation, public key hashing, X.509 cert with embedded attestation extension. |
| `session_manager.proto` | Protobuf service definition for `AgentBrokerService`. Currently `Echo`; will be extended. |
| `BUILD` | Bazel build rules for proto, gRPC stubs, TLS cert lib, and server binary. |
| `MODULE.bazel` | Bazel module definition with external deps (gRPC, Abseil, BoringSSL, Protobuf). |
| `Dockerfile` | Multi-stage Docker build (Ubuntu 24.04 + Bazelisk → minimal runtime). |
| `run_server.sh` | Helper to build the Docker image and run the container. |
| `.bazelversion` | Pins Bazel to 7.6.1 via Bazelisk. |
| `.dockerignore` | Excludes `bazel-*` and `.git` from Docker context. |

## Building

### Via Docker (recommended)

From the repository root:

```bash
cd tee
./run_server.sh [port]       # default port: 8000
./run_server.sh 8000 -d      # detached mode
```

This builds the server inside a hermetic Docker container using
Bazelisk and produces a minimal runtime image (~50 MB)
containing only the static binary and CA certificates.

### Local Bazel Build (development)

On some Linux distributions with GCC 15+ and binutils 2.45+,
you may hit a known `.sframe` linker bug. Use Clang as a
workaround:

```bash
cd tee
CC=clang CXX=clang++ bazel build :ztab_server
```

Build the specific `:ztab_server` target — do **not** use
`//...`, as it will attempt to compile external test targets
that may not be available locally.

The resulting binary is at `bazel-bin/ztab_server`.

### Running the Binary Directly

```bash
./bazel-bin/ztab_server [port]   # default: 8000
```

## Dependencies

All dependencies are fetched automatically by Bazel via
`MODULE.bazel`:

| Dependency | Version | Purpose |
| :--- | :--- | :--- |
| gRPC | 1.72.0 | RPC framework and TLS credentials |
| Abseil | 20250127.1 | Status, logging, string utilities |
| BoringSSL | (via gRPC) | EC keys, X.509 certs |
| Protobuf | 30.2 | Proto compilation and runtime |

## Architecture Notes

### Mock vs. Production Attestation

In the current development configuration, the server generates
a **mock attestation token** — an unsigned JWT (`alg: none`)
with hardcoded claims simulating a GCP Confidential Space
attestation report:

```json
{
  "iss": "https://confidentialcomputing.googleapis.com",
  "aud": "ztab_tls",
  "dbgstat": "disabled-since-boot",
  "secboot": true,
  "hwmodel": "GCP_INTEL_TDX",
  "submods": {
    "container": {
      "image_digest": "sha256:000...000"
    }
  },
  "eat_nonce": "<base64url(SHA-256(public_key))>"
}
```

In production, this will be replaced by a real token fetched
from the Confidential Space metadata endpoint, signed by
Google's attestation service, and verifiable via Google's
public OIDC keys.

### Key Binding

The `eat_nonce` claim in the attestation token contains the
Base64URL-encoded SHA-256 hash of the server's ephemeral TLS
public key. This cryptographic binding ensures that:

*   The attestation report is tied to a specific key pair.
*   An attacker cannot substitute a different public key
    without invalidating the attestation.
*   The client can verify that the TLS connection terminates
    inside the attested TEE.

### Future: Session Management

The `AgentBrokerService` will be extended with session
lifecycle RPCs: `CreateSession`, `JoinSession`, `SubmitInput`,
`GetOutcome`. These will coordinate multi-party private LLM
sessions where agents from different entities submit private
inputs to a shared Gemma model running inside the TEE.
