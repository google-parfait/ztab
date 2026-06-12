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
| `main.cc` | Server entry point. Key generation, attestation, certificate creation, gRPC startup. Contains `AgentBrokerService` (Echo + LLM inference). |
| `tls_cert_generator.h/.cc` | `EphemeralCredentialGenerator` class. EC key generation, public key hashing, X.509 cert with embedded attestation extension. |
| `llama_engine.h/.cc` | LLM inference engine wrapping llama.cpp. Loads GGUF models and generates completions. |
| `session_manager.proto` | Protobuf service definition for `AgentBrokerService`. Currently `Echo`; will be extended. |
| `BUILD` | Bazel build rules for proto, gRPC stubs, TLS cert lib, server binary, and OCI image targets. |
| `MODULE.bazel` | Bazel module definition with external deps and OCI image packaging. |
| `model_targets.bzl` | Macro generating per-model OCI image targets (local and GCP variants). |
| `repo_rules.bzl` | `gcs_file` repository rule for downloading model weights from GCS at analysis time. |
| `build_defs.bzl` | `define_load_runner` genrule for `bazel run` → `docker load` workflow. |
| `run_server.sh` | Helper to build the OCI image and run the container locally via Docker. |
| `.bazelversion` | Pins Bazel to 8.2.1 via Bazelisk. |

## Building

### OCI Image (recommended for Docker / GCP deployment)

The server is packaged as an OCI image using Bazel's `rules_oci`
(no Dockerfile). The base image is `distroless/cc-debian12`.

```bash
cd tee

# Echo-only (no model, quick test):
./run_server.sh

# With LLM (downloads Gemma 4 E2B from your GCS bucket):
./run_server.sh --llm --gcs_bucket gs://your-model-bucket

# With a specific model:
./run_server.sh --model gemma4_e4b --gcs_bucket gs://your-model-bucket

# With GPU passthrough:
./run_server.sh --model gemma4_e4b --gcs_bucket gs://your-model-bucket --gpu

# Custom port, detached:
./run_server.sh --port 9000 -d
```

Model weights are downloaded from GCS via Bazel's `gcs_file`
repository rule and cached automatically. The `--gcs_bucket` flag
is required when using `--llm` or `--model`.

```bash
bazelisk build --repo_env=GCS_MODEL_BUCKET=gs://your-bucket :model_layer_gemma4_e4b
```

### Local Bazel Build (development)

```bash
cd tee
bazelisk build :ztab_server
```

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
| gRPC | 1.78.0 | RPC framework and TLS credentials |
| Abseil | 20250814.0 | Status, logging, string utilities |
| BoringSSL | 0.20260211.0 | EC keys, X.509 certs |
| Protobuf | 33.0-rc2 | Proto compilation and runtime |
| llama.cpp | b8875 | LLM inference engine (Gemma 4 support) |
| rules_oci | 2.2.6 | OCI image packaging |
| rules_pkg | 1.1.0 | `pkg_tar` for image layering |
| CUDA 12.2 | (optional) | GPU inference via `--//:enable_cuda=true` |

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
