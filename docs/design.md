# ZTAB Design Document

> **Status:** Draft — sections being filled incrementally.
>
> **Last updated:** 2026-06-30

## 1. Introduction

### 1.1 What is ZTAB?

ZTAB is a broker that runs inside a Trusted
Execution Environment (TEE), allowing autonomous agents representing
different, mutually distrustful entities to securely coordinate,
exchange sensitive data, and negotiate shared outcomes.

By combining TEEs with encapsulated LLMs, ZTAB enables agents to
submit private inputs to a shared session and receive verified common
outcomes — without exposing their raw inputs to other participants or
to the cloud infrastructure provider.

### 1.2 Motivating Problem

Autonomous agents are moving from simple information retrieval to
high-stakes economic activity like payments, contracts, negotiations,
and joint decision-making. This creates a **trust ceiling**: there is
currently no neutral environment where agents from mutually distrustful
entities can securely exchange sensitive data, verify commitments, or
negotiate outcomes.

All existing solutions secure **bilateral** relationships — one agent
talking to one tool or platform. But real-world coordination requires
**multilateral** interactions where private data from multiple parties
must be pooled and reasoned over, without any party seeing another's
raw inputs, and without trusting the infrastructure operator.

Existing approaches each fall short:

- **Standard API calls** expose data to the service provider.
- **Encrypted communication** (TLS) protects data in transit but not
  at the processing endpoint.
- **Multi-Party Computation (MPC)** provides theoretical guarantees
  but cannot run arbitrary LLM inference.
- **Data Clean Rooms** support SQL-level analysis but not LLM-based
  reasoning, and are passive rather than agent-native.
- **Confidential inference** protects users from the inference
  provider, but is unidirectional — it doesn't protect agents from
  each other.

ZTAB addresses this gap: it is a **neutral ground** where competing
agents can submit private inputs to a shared LLM running inside
hardware-protected memory, and receive verified common outcomes.

This need is amplified by emerging regulatory trends (e.g., the EU AI
Act) that increasingly require transparency and auditability for
autonomous agent interactions — creating structural demand for
verifiable coordination infrastructure.

### 1.3 Key Design Goals

1. **Privacy**: No participant can see another's raw input. Private
   inputs must not be visible to other participants, the broker
   operator, or the cloud infrastructure provider.

2. **Verifiability**: Before transmitting any data, clients can
   cryptographically verify the server's hardware and software state
   via Remote Attestation.

3. **Agent-native**: ZTAB integrates with autonomous agent workflows
   via the Model Context Protocol (MCP). Agents discover and use ZTAB
   through tool descriptions — no integration code required. The
   bootstrapping mechanism is designed for zero friction: agents
   autonomously discover and launch the ZTAB MCP server (via
   framework-native plugin orchestration), guided solely by the
   tool's `SKILL.md` description. No human-authored integration code,
   wrapper scripts, or manual configuration is needed — the agent
   reads the instructions and self-configures.

4. **Minimal trust**: Trust the hardware attestation, not the
   operator. The security model assumes the cloud provider, host
   hypervisor, and other tenants are all potential adversaries.

5. **Cloud-portable**: The core system is cloud-agnostic, with a
   pluggable attestation provider interface. GCP Confidential Space
   (Intel TDX + H100 GPU) is the reference deployment target.

6. **Incremental adoption**: Production deployments use real TEE
   hardware with genuine attestation (`--verifier ita`), where
   connections are verified against Intel Trust Authority on TDX.
   For local development and testing without TEE hardware, a mock
   attestation mode (`--verifier noop`) is available but provides
   **no security guarantees** and must never be used for production
   deployments or for processing real sensitive data.

7. **Policy-as-code**: Processing templates (prompts, schemas) are
   part of the container image. Changing a policy changes the
   attestation digest, enabling cryptographic verification of
   exactly what processing logic the TEE is running.

### 1.4 Motivating Use Cases

The following scenarios illustrate the types of cooperative multilateral
coordination ZTAB enables in its current release:

- **Private Schedule Coordination**: Agents from different
  organizations each submit their principal's availability. The TEE
  computes overlapping free slots and returns only the common
  windows — no participant sees another's full calendar. (This is
  the reference scenario implemented in `examples/calendar/`.)

TODO: b/438809953 - flesh out additional use cases as examples and
document them in this section

## 2. System Architecture

### 2.1 High-Level Overview

ZTAB consists of two primary components:

1. **TEE Server** (`tee/`): A C++ gRPC server intended for deployment
   inside a GCP Confidential Space VM with Intel TDX. It manages
   sessions, enforces policies, runs LLM inference (via llama.cpp),
   and presents attestation evidence to clients via RA-TLS.

2. **Agent Client** (`agent/`): A Python client library, CLI, and MCP
   server that runs on the agent's machine. It establishes verified
   connections to the TEE server, and exposes ZTAB capabilities to
   agent frameworks (e.g., Gemini CLI, Antigravity) via the Model
   Context Protocol (MCP).

```
┌─────────────────┐      RA-TLS       ┌──────────────────────────┐      RA-TLS       ┌─────────────────┐
│  Agent A        │   gRPC / TLS 1.3  │    ZTAB TEE Server       │   gRPC / TLS 1.3  │  Agent B        │
│                 │◄─────────────────►│    (Confidential Space)  │◄─────────────────►│                 │
│ ┌─────────────┐ │                   │                          │                   │ ┌─────────────┐ │
│ │ MCP Server  │ │                   │  ┌────────────────────┐  │                   │ │ MCP Server  │ │
│ │ (local,     │ │                   │  │ TlsProxy (RA-TLS)  │  │                   │ │ (local,     │ │
│ │  handles    │ │                   │  ├────────────────────┤  │                   │ │  handles    │ │
│ │  attestation│ │                   │  │ SessionManager     │  │                   │ │  attestation│ │
│ │  for agent) │ │                   │  │ PolicyRegistry     │  │                   │ │  for agent) │ │
│ ├─────────────┤ │                   │  │ LlamaEngine        │  │                   │ ├─────────────┤ │
│ │ client.py   │ │                   │  └────────────────────┘  │                   │ │ client.py   │ │
│ │ ZtabChannel │ │                   │                          │                   │ │ ZtabChannel │ │
│ └─────────────┘ │                   │  Hardware-isolated        │                   │ └─────────────┘ │
└─────────────────┘                   │  encrypted memory (TDX)  │                   └─────────────────┘
                                      └──────────────────────────┘
```

The MCP server runs **locally on each agent's machine**, not inside
the TEE. This is deliberate: the MCP server handles attestation
verification on behalf of the agent, shielding the agent from the
complexities of RA-TLS, certificate parsing, and JWT verification.
If the MCP server were hosted remotely (e.g., on a separate machine),
it would itself need to be attested — creating a recursive trust
problem. By running locally as part of the agent's client stack,
the MCP server is within the agent's own trust boundary.

### 2.2 Why gRPC (Client-to-TEE Protocol Selection)

The protocol between the agent client library and the TEE server was
selected from three candidates:

1. **Simple HTTP/REST** — Rejected because it lacks bidirectional
   streaming, HTTP/2 multiplexing, and typed schema enforcement.
   REST would require hand-rolling request/response schemas with no
   compile-time type safety.
2. **Nesting a secondary MCP server inside the TEE** — Rejected
   because MCP uses stdio transport (stdin/stdout), which is
   fundamentally unsuitable for remote network communication. An MCP
   server inside the TEE would require a separate network transport
   bridge, adding unnecessary complexity.
3. **gRPC with Protobuf (selected)** — Provides typed schema
   enforcement via `.proto` definitions, native HTTP/2 multiplexing
   (critical for channel caching — see [§2.4](#24-connection-model-and-channel-caching)),
   and built-in TLS support that the RA-TLS proxy integrates with
   cleanly.

**Rejected alternative — bidirectional streaming:** A feasibility
analysis considered migrating to bidirectional streaming,
but this was rejected because it breaks gRPC's native per-RPC error
propagation and debuggability. The unary RPC model with channel
caching provides equivalent performance without multiplexing
complexity.

**Rejected alternative — C++ client subprocess:** The client was
initially prototyped as a C++ binary (sharing code with the TEE
server) wrapped by a Python subprocess. This was rejected in favor
of a pure Python implementation to eliminate native compilation
requirements for client users — the agent code is plain Python
and can be run directly without any installation step.

### 2.3 Why an In-Process TLS Proxy

The TLS proxy is the most unusual component in the architecture, and its
existence requires explanation. The canonical RA-TLS pattern (used by
Gramine, Open Enclave, Intel SGX SDK, etc.) generates a fresh attestation
token and ephemeral certificate **per client connection**, during the TLS
handshake itself. This ensures the attestation evidence is always fresh
and cryptographically bound to the specific TLS session.

The standard way to achieve this in BoringSSL is via the `SSL_CTX_set_cert_cb()`
callback API, which pauses the TLS handshake, allows the application to
generate credentials on-demand, and then resumes. However, **gRPC's C++
wrapper hides the raw `SSL_CTX` and `SSL` handles**. The public credentials
API (`TlsServerCredentialsOptions`) only allows configuring a
`CertificateProviderInterface`, which is polled at startup or via
filesystem watching — it does not expose a per-handshake callback.

This means gRPC C++ cannot implement the canonical RA-TLS pattern directly.
Specifically, gRPC 1.78.0 lacks an `InMemoryCertificateProvider`
that would allow injecting programmatically generated certificates,
and the gRPC Core caches the certificate provider pointer at startup,
preventing dynamic rotation.
Rather than inventing a bespoke protocol (e.g., moving attestation to an
explicit RPC), ZTAB implements the standard BoringSSL pattern by bypassing
gRPC's TLS layer:

1. **TlsProxy** listens on the public port (e.g., 8000) using raw
   BoringSSL `SSL_accept()`.
2. On each incoming connection, it fetches a fresh attestation token,
   generates an ephemeral P-256 key and self-signed X.509 certificate,
   and performs the TLS 1.3 handshake.
3. The decrypted traffic is piped to gRPC running in **insecure mode on
   the loopback interface** (`127.0.0.1:8001`).

**IPC rationale:** In the Confidential Space deployment, the gRPC
backend and TlsProxy are the only processes inside the attested
container image. The container digest is verified by clients via RA-TLS
(§3.2); adding any additional process would change the digest and fail
attestation. Loopback traffic in a TDX guest is handled entirely within
the guest kernel's network stack and remains in private (encrypted)
memory — it never traverses the virtio-net interface or enters shared
memory visible to the hypervisor. A Unix Domain Socket would add
operational complexity (socket lifecycle, crash cleanup) without closing
any attack vector present in the threat model. For non-TEE local development,
the service uses mock attestation and processes no real secrets, rendering
local port squatting or IPC mitigations unnecessary.

This achieves the exact same zero-cache, on-demand attestation pattern as
Oak's Noise protocol, using standard BoringSSL TLS 1.3, without modifying
gRPC core. The proxy includes a 10-second credential cache to handle the
client's "double connect" pattern (pre-flight cert fetch followed
immediately by gRPC channel setup), avoiding redundant attestation token
requests.

**Concurrency:** The proxy supports up to 50 concurrent connections, with
each connection handled asynchronously via `std::future`. Active socket
tracking and a throttling condition variable prevent resource exhaustion.

### 2.4 Connection Model and Channel Caching

Early GCP deployments revealed a **329-connection storm**: because the
Python MCP server created a fresh `ZtabChannel` (and thus a fresh TLS
handshake with attestation verification) for every single MCP tool call,
a typical multi-step agent session generated hundreds of redundant TLS
connections. Each connection required a round-trip to the Intel Trust
Authority for a fresh attestation token.

The fix was **channel caching** in `mcp_server.py`: gRPC channels are
cached per backend ID and reused across tool calls. HTTP/2 multiplexing
ensures all RPCs share a single TCP connection. The backends file's
`mtime` is checked on each access; if the config file has changed, stale
channels are flushed and new connections are established on the next call.

This is architecturally sound because attestation verification happens at
the TLS layer during the initial handshake. Once the secure channel is
established, all subsequent RPCs are protected by the TLS 1.3 session
keys — re-verifying attestation per RPC would be cryptographically
redundant (see `attestation_architecture_investigation.md` §3 for the
full analysis).

### 2.5 Component Inventory

| Component | Language | Location | Description |
| :--- | :--- | :--- | :--- |
| **TEE gRPC Server** | C++ | `tee/main.cc` | Entry point. Wires together LlamaEngine, PolicyRegistry, SessionManager, TlsProxy, and optional admission control (`--creator_token`). |
| **Session Manager** | C++ | `tee/session_manager.{h,cc}` | Multi-agent session lifecycle: create, join, accept, submit, get-result. Thread-safe with lazy timeout enforcement. |
| **Policy Registry** | C++ | `tee/policy_registry.{h,cc}` | Loads policy definitions (prompt templates + JSON schemas) from disk at startup. |
| **LLM Engine** | C++ | `tee/llama_engine.{h,cc}` | Wraps llama.cpp for GGUF model inference. Supports CPU and GPU offload. |
| **TLS Proxy** | C++ | `tee/tls_proxy.{h,cc}` | TLS-terminating reverse proxy implementing the RA-TLS pattern. Generates per-connection attestation-bound certificates. See [Section 2.3](#23-why-an-in-process-tls-proxy) for architectural rationale. |
| **Cert Generator** | C++ | `tee/tls_cert_generator.{h,cc}` | Generates ephemeral NIST P-256 keys and self-signed X.509 certs with embedded attestation tokens. |
| **Attestation Providers** | C++ | `tee/*_attestation_token_provider.{h,cc}` | Abstract interface + mock (dev) and GCP/ITA (prod) implementations for obtaining attestation JWTs. |
| **Proto Definition** | Protobuf | `tee/session_manager.proto` | Service definition for `AgentBrokerService` (Echo + session RPCs). |
| **Python Client** | Python | `agent/client.py` | `ZtabChannel`: TLS cert fetching, attestation extraction, gRPC channel setup with pluggable verifiers. |
| **MCP Server** | Python | `agent/mcp_server.py` | Stdio-based MCP server exposing ZTAB tools. Named backend resolution, channel caching, lazy config reloading. |
| **CLI** | Python | `agent/cli.py` | Standalone command-line tool for all session RPCs. |
| **Attestation Utils** | Python | `agent/attestation.py` | Extracts attestation tokens from X.509 certificate extensions (OID parsing, ASN.1 unwrapping). |
| **ITA Verifier** | Python | `agent/ita_verifier.py` | Full Intel Trust Authority JWT verification: signature, claims, key-binding, `swname`, `cvm_compliance_status`, and container digest. |
| **Verifier Factory** | Python | `agent/verifier_factory.py` | Factory that validates a `VerifierPolicy` (`ItaPolicy`, `NoopPolicy`) and returns the configured verifier callable. |
| **GCP Deployment** | Shell/Bzl | `gcp/` | Scripts and Bazel macros for building, pushing, and launching on GCP Confidential Space. |

## 3. Security Architecture

### 3.1 Threat Model

**What we trust:**

- **TEE hardware** (Intel TDX): Provides hardware-level memory
  encryption and isolation. The CPU enforces that even a compromised
  hypervisor cannot read TEE memory.
- **Attestation service** (Intel Trust Authority / GCP Confidential
  Computing): Provides signed attestation reports that clients can
  verify.
- **The ZTAB server code itself**: Since it is open-source, auditable,
  and its hash is included in the attestation report.

**What we do NOT trust:**

- **The cloud provider / host hypervisor**: Cannot read TEE memory,
  but could deny service.
- **The broker operator**: Cannot observe or modify session data
  inside the TEE.
- **Other participants**: Each agent submits private inputs; no agent
  can see another's raw data.
- **The network**: All communication is over TLS 1.3; attestation
  tokens are cryptographically bound to the TLS key.
- **The agents themselves**: Agents are assumed potentially
  adversarial. The system prevents prompt injection, input data
  exfiltration, and attestation bypass via architectural controls.

**Why sessions are ephemeral (threat taxonomy):**

ZTAB's sessions are **ephemeral** — each session starts from a clean
slate with no persistent memory, RAG knowledge bases, or cross-session
state, and all session data is discarded upon completion or timeout.
This is an intentional security design choice, not merely a
simplification. It directly mitigates two classes of attacks from the
AI agent threat taxonomy:

- **Cognitive state attacks**: Memory poisoning across sessions, where
  an adversary corrupts persistent agent memory in one session to
  manipulate behavior in future sessions.
- **Systemic attacks**: Multi-agent collusion via shared persistent
  state, where agents coordinate malicious behavior through a shared
  memory layer.

Persistent agent architectures are fundamentally vulnerable to these
attack classes. By discarding all state after each session, ZTAB
eliminates the attack surface entirely.

**Input isolation (prompt injection defense):**

Participant inputs are validated against a JSON Schema defined by
the policy before being incorporated into the LLM prompt (see
[§3.6](#36-inputoutput-schema-validation)). This input schema
validation is a critical defense against prompt injection: by
constraining inputs to structured data (e.g., JSON objects with
typed fields and regex-validated strings), the system prevents
participants from injecting arbitrary natural language instructions
that could manipulate the LLM's behavior or extract other
participants' private data.

**Security requirements:**

| ID | Requirement |
| :--- | :--- |
| R-SEC-1 | Private inputs MUST NOT be visible to other participants. |
| R-SEC-2 | Private inputs MUST NOT be visible to the broker operator. |
| R-SEC-3 | Private inputs MUST NOT persist beyond session lifetime. |
| R-SEC-4 | TEE attestation MUST be verified before any data transmission. |
| R-SEC-5 | Output MUST be sanitized to prevent private input leakage. |
| R-SEC-6 | Session isolation: session A data MUST NOT leak to session B. |
| R-SEC-7 | Session creation MUST be gatable via a static creator token to prevent unauthorized resource consumption. |

### 3.2 RA-TLS (Remote Attestation over TLS)

ZTAB uses a standard TLS-terminating reverse proxy pattern implementing
RA-TLS (Remote Attestation over TLS), similar to how nginx or envoy
terminates TLS but with attestation evidence embedded in the
certificate.

**Standards used (no bespoke cryptography):**

- TLS 1.3 via BoringSSL (RFC 8446)
- X.509v3 self-signed certificates (RFC 5280)
- NIST P-256 / secp256r1 ephemeral keys (FIPS 186-4)
- SHA-256 key hashing for EAT nonce binding (RFC 9334 RATS)
- HTTP/2 ALPN negotiation (RFC 7301) for gRPC compatibility

**Server-side flow (per-connection):**

1. Generate an ephemeral NIST P-256 key pair.
2. Compute SHA-256 hash of the public key (SubjectPublicKeyInfo DER).
3. Base64URL-encode the hash to produce the EAT nonce.
4. Request an attestation token (JWT) from the attestation provider,
   passing the nonce. In production, this goes to the GCP Confidential
   Space metadata agent which contacts Intel Trust Authority.
5. Generate a self-signed X.509 certificate embedding the JWT in a
   custom extension (OID `1.3.6.1.4.1.99999.1`). **Note:** This OID
   is a development placeholder under the unassigned private enterprise
   arc. Production deployments should register a proper Private
   Enterprise Number (PEN) with IANA.
6. Use the certificate for the TLS handshake.

**Client-side verification flow:**

1. Open a raw TLS connection (accepting self-signed certs).
2. Extract the server's X.509 certificate.
3. Parse the custom extension OID to extract the attestation JWT.
4. Strip the ASN.1 OCTET STRING wrapper.
5. Verify the JWT:
   - Signature (against ITA/GCA JWKS public keys)
   - Issuer (`iss`)
   - Audience (`aud`)
   - Expiration (`exp`, `nbf`)
   - Hardware model (`hwmodel == INTEL_TDX`)
   - Secure boot (`secboot == true`)
   - Debug status (`dbgstat ∈ {disabled, disabled-since-boot}`).
     When `allow_debug_tee` is set in the backend configuration,
     `enabled` is also accepted — this supports GCP Confidential
     Space preview and development images that report an `enabled`
     debug status.
6. Verify key binding: `eat_nonce == Base64URL(SHA-256(cert_pubkey))`
7. Optionally verify container image digest.
8. If all checks pass, establish gRPC channel using the cert as
   trusted root.

**Clock skew tolerance:** JWT time-based claims (`exp`, `nbf`) are
verified with a 300-second clock skew tolerance to accommodate
transient clock drift on freshly booted cloud VMs.

**Attestation freshness properties:**

The current nonce `Base64URL(SHA-256(cert_pubkey))` provides
**key binding**: the attestation token is cryptographically bound
to the server's ephemeral TLS key. A valid token cannot be reused
with a different key pair. Combined with ephemeral per-connection
key generation and the short token lifetime (~5–10 minutes),
this limits the window during which a captured token could
theoretically be replayed.

However, the current mechanism does **not** provide interactive
client-verifiable freshness. There is no challenge-response
element — the nonce depends only on the server's key, not on any
client-supplied entropy. A future improvement will incorporate
`ClientHello.random` into the nonce computation (composite nonce)
to bind the attestation to each individual TLS session, or use
TLS Keying Material Export (RFC 5705) for full channel binding.

**Container image digest verification:**

> **⚠️ IMPORTANT:** Client-side verification of the container
> image digest (`submods.container.image_digest` in the
> attestation token) is the critical component for establishing
> trust in the server's identity. Without it, any GCP
> Confidential Space workload running on matching hardware can
> obtain a valid attestation token — the client cannot
> distinguish the legitimate ZTAB server from a malicious
> workload. Callers **must** provide an `expected_image_digest`
> when creating the `ItaPolicy` for production use. Omitting
> the digest check is only appropriate for local development
> and testing. For full Cloud Foundation Core (CFC) parity,
> callers should also specify `--expected_project_id` and
> `--expected_service_account` to strongly bind the workload
> identity to a known deployment.

### 3.3 Attestation Providers

Two attestation provider implementations are supported. The `mock`
provider is for **local development and testing only** and provides
no security guarantees. The `ita` provider is the production
implementation.

| Provider | Flag | Use Case | Token Type |
| :--- | :--- | :--- | :--- |
| `mock` | `--attestation_provider=mock` | Local development only | Unsigned JWT (`alg: none`) with hardcoded claims |
| `ita` | `--attestation_provider=ita` | GCP Confidential Space (production) | Signed JWT from Intel Trust Authority via GCP metadata agent |

**Attestation protocol selection — RA-TLS vs. Oak Noise:**

Two attestation transport mechanisms were evaluated for binding
hardware attestation evidence to the client-server TLS channel:
(1) **RA-TLS** (embedding attestation JWTs in self-signed X.509
certificates), and (2) **Oak's Noise NK protocol** (a custom
Noise-based handshake with attestation binding). ZTAB chose RA-TLS.
The rationale follows.

ZTAB uses standard RA-TLS deliberately instead of Oak's Noise NK
protocol. The primary motivation is **client integration simplicity**.
ZTAB's Python client requires only `pip install grpcio cryptography
pyjwt` — no Bazel, Rust toolchain, or compilation needed. Integrating
with Oak would require either maintaining a Python FFI bridge to Rust
Oak libraries (complex, fragile), reimplementing Noise NK in Python
(actual bespoke crypto), or running an Oak proxy sidecar (operational
complexity). No maintained Python Oak client exists.

Critically, TLS 1.3 and Noise NK provide **equivalent security
properties** for this use case:

- **Key binding**: Both bind the attestation evidence to the
  transport-layer public key (via EAT nonce in RA-TLS, handshake
  hash in Noise NK).
- **Forward secrecy**: Both achieve it via ephemeral key exchange
  (mandatory ECDHE in TLS 1.3, ephemeral DH in Noise).
- **Channel binding**: Both prevent session hijacking after handshake.

The key trade-off is **token freshness**: Oak generates raw TDX/SGX
quotes (no built-in TTL) per handshake, while ZTAB uses ITA JWTs
(short-lived OIDC tokens). JWTs were chosen because they can be
verified in Python with standard libraries (`pyjwt` + JWKS endpoint),
rather than requiring the Intel DCAP C library for raw quote
verification. This is the core integration simplicity trade-off.
The `ita_verifier.py` maintains a 24-hour TTL cache for JWKS
public keys to survive Intel key rotations without re-fetching on
every verification.

The total custom code footprint for the RA-TLS implementation is
approximately 185 lines — all socket plumbing and lifecycle
orchestration, zero custom cryptographic primitives. Every crypto
operation delegates to BoringSSL (server) or `cryptography`/`pyjwt`
(client).

RA-TLS is a well-established pattern used by Gramine, Open Enclave SDK,
Intel SGX SDK, Asylo (Google), and Fortanix EDP.

**Rejected alternative — post-handshake attestation:** An alternative
pattern where attestation occurs as an application-layer RPC *after*
the TLS connection is established was evaluated and rejected. This
pattern deviates from the standard X.509 certificate model, requires
custom client-side protocol logic, and undermines the integration
simplicity goal (clients would need a custom attestation exchange
library in addition to standard TLS).

### 3.4 Policy as Part of the Trusted Codebase

Policies are loaded from JSON files on disk at server startup via
`PolicyRegistry::LoadFromDirectory()`. This is intentional — policy
text (prompt templates, schemas) is part of the **trusted codebase**.

For container images deployed in GCP Confidential Space, the policy
files are included in the image. Changing a policy changes the
container image digest, which in turn changes the attestation token's
`image_digest` claim. Clients can verify this claim to confirm the
TEE is running an approved set of policies.

This design provides a strong security guarantee: agents can
cryptographically verify not just that the server is running inside
a TEE, but that the TEE is running a specific, auditable set of
processing logic.

**Rejected alternative — dynamic policy loading:** An alternative
design considered loading policies dynamically from cloud storage
(e.g., GCS) at runtime, which would allow policy updates without
rebuilding the container image. This was explicitly rejected because
dynamically loaded policies would not be covered by the container
image digest in the attestation token — clients would have no way to
cryptographically verify which policies the TEE is actually running.
Static policy embedding is a deliberate security trade-off: policy
updates require a new container build, but every policy is attestable.

**Future extensibility:** A planned extension could allow agents to
define ad-hoc policies at session creation time (passing the prompt
template in the `CreateSession` RPC). This would weaken the security
model because arbitrary prompts from untrusted clients would bypass
attestation-based policy verification. If implemented, it should be
gated by an explicit `--allow_adhoc_policies` flag and documented
as a trust trade-off.

### 3.5 Input and Output Leakage Prevention

The system employs multiple defenses at both the input and output
boundaries to prevent prompt injection and private data leakage:

0. **Input schema validation**: Before participant inputs are
   incorporated into the LLM prompt, they are validated against a
   JSON Schema defined by the policy (see [§3.6](#36-inputoutput-schema-validation)).
   This constrains inputs to structured data, preventing participants
   from injecting arbitrary natural language instructions into the
   prompt. Inputs that fail validation are rejected with
   `INPUT_SCHEMA_VIOLATION`.

1. **Prompt-level instructions**: Policy prompt templates explicitly
   instruct the LLM not to reveal individual participants' data.
   For example, the `ScheduleOverlap` prompt states: *"Each
   participant's data is private — you MUST NOT reveal any
   individual's schedule in your output."*

2. **Output schema validation**: LLM outputs are validated against
   a JSON Schema with regex patterns (e.g., only ISO 8601 datetime
   strings allowed). This constrains the output space to prevent
   the LLM from embedding raw input data in its response.

3. **Randomized delimiters**: When constructing the aggregated
   prompt from participant inputs, the session manager generates
   random hex suffixes for the delimiter tags (e.g.,
   `<<<PARTICIPANT_1_INPUT_BEGIN_a3f7c2e1>>>`). This prevents
   prompt injection attacks where a participant crafts an input
   that mimics delimiter tags to manipulate the LLM's interpretation
   of the prompt structure.

4. **Error code for detected leakage**: The protocol includes an
   `OUTPUT_LEAKAGE_DETECTED` error code for future implementation
   of runtime leakage detection heuristics.

**Planned: Non-Neural Output Guard.** The planned runtime leakage
detection compiles candidate sensitive values (extracted from
participant inputs) into an in-memory Aho-Corasick Trie for
single-pass stream scanning of LLM output. This is a non-neural,
deterministic mechanism chosen for predictability and auditability
over ML-based classifiers. Policies can declare `exempt_fields` to
allowlist public identifiers (e.g., city names, standard nomenclature)
that should not trigger false-positive leakage alerts.

**Semantic leakage — a fundamental limitation:** Semantic leakage —
where the LLM paraphrases or summarizes private data rather than
reproducing it verbatim — is a fundamental limitation of any
LLM-based system that no substring scanner can catch. The primary
mitigation is structurally restricting output schemas to
low-semantic-content types (e.g., datetime arrays, numeric values,
boolean flags) that cannot encode arbitrary natural language.

**Note:** Full runtime output leakage detection is architecturally
planned but not yet implemented in the current codebase.

### 3.6 Input/Output Schema Validation

The session manager implements JSON Schema validation for both inputs
and outputs, supporting:

- **Type checking** (`string`, `object`, `array`, `integer`, etc.)
- **Regex pattern matching** (`pattern` field) using RE2 (linear-time,
  safe from ReDoS attacks)
- **Object validation** (`properties`, `required`, `additionalProperties`)
- **Array bounds** (`minItems`, `maxItems`)

Input validation ensures that participants can only submit data in
the format expected by the policy's prompt template, preventing
malformed inputs from causing unexpected LLM behavior.

Output validation is a critical defense layer: by requiring LLM
outputs to match a strict schema with regex patterns, the system
restricts the LLM's output space and makes it difficult for an
adversarial prompt to cause the LLM to embed arbitrary data (such
as another participant's raw input) in the output.

For example, the `ScheduleOverlap` policy's output schema allows
only an array of strings matching the ISO 8601 datetime regex
`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$`, with a maximum of
200 items. The LLM cannot output free-text containing other
participants' schedules — only valid datetime strings.

If the LLM output fails schema validation, the session manager
retries inference up to 3 times. If all retries fail, the session
transitions to `ABORTED` with error code `OUTPUT_SCHEMA_VIOLATION`.

### 3.7 Observed Attack Patterns

During multi-agent E2E testing, six jailbreak patterns were
identified and mitigated:

| ID | Pattern | Description | Mitigation |
| :--- | :--- | :--- | :--- |
| J1 | **Cross-agent snooping** | Agent reads files from another agent's sandbox directory. | Per-agent `$HOME` directories (current); Docker-based per-agent containerization (planned, §8.1). In production, agents run on separate machines with no shared filesystem. |
| J2 | **Cross-agent tampering** | Agent writes to another agent's sandbox to plant false state. | Same directory isolation (current); Docker enforcement planned (§8.1). In production, inter-agent communication is exclusively via RA-TLS to the TEE. |
| J3 | **Token theft** | Agent extracts `participant_token` from another agent's logs or environment. | Token passed via env var (not CLI arg); log sanitization; isolated sandboxes. Token is ephemeral and session-scoped (see §4.4). |
| J4 | **Session fabrication** | Agent hallucinates an `invitation_token` without calling the TEE, skipping attestation. | Cryptographically random 256-bit tokens; server rejects unknown tokens. |
| J5 | **Attestation bypass** | Agent passes `verifier="noop"` to skip RA-TLS verification for remote hosts. | Named Backends: verifier is read from config file, not tool arguments. Remote hosts always enforce `ita`. |
| J6 | **Unauthorized session creation** | Attacker creates sessions on a production server without authorization, consuming resources. | Admission control: `--creator_token` flag gates `CreateSession`. Server rejects requests without a valid token with `PERMISSION_DENIED`. |

These patterns informed the security design of Named Backends (§6.3),
the sandbox isolation model (§8.1), and the trajectory audit
framework (§8.1).

## 4. Session Protocol

### 4.1 Session State Machine

Sessions follow a strict state machine:

```
  OPEN ──────► OPEN ──────► SEALED ──────► CALCULATING
   │            │              │               │
   │timeout     │timeout       │timeout        ├── success ──► CLOSED
   ▼            ▼              ▼               │
 ABORTED     ABORTED        ABORTED            └── failure ──► ABORTED

 CreateSession  JoinSession   AcceptPolicy     SubmitInput
                (all joined)  (all accepted)   (all submitted)
```

**States:**

| State | Value | Description |
| :--- | :--- | :--- |
| `OPEN` | 1 | Waiting for participants to join and accept the policy. |
| `SEALED` | 2 | All participants have joined and accepted. Accepting inputs. |
| `CALCULATING` | 3 | All inputs received. LLM inference in progress. |
| `CLOSED` | 4 | Result available. Terminal state. |
| `ABORTED` | 5 | Session failed or timed out. Terminal state. |

### 4.2 Session Lifecycle RPCs

| RPC | When | What Happens |
| :--- | :--- | :--- |
| `CreateSession` | Initiator creates a session | Policy looked up in registry, session created in `OPEN` state. Creator gets `invitation_token` (to share) + `participant_token` (secret). |
| `JoinSession` | Other participants join via `invitation_token` | Participant receives the policy for review + own `participant_token`. Session stays `OPEN`. |
| `AcceptPolicy` | Each participant accepts | When all participants accept → transitions to `SEALED`. |
| `SubmitInput` | Each participant submits private data | Input validated against JSON Schema. When all inputs received → transitions to `CALCULATING`. |
| `GetResult` | Any participant polls for result | Returns result JSON if `CLOSED`, error info if `ABORTED`, current state otherwise. |
| `GetSessionStatus` | Any participant checks progress | Returns join/accept/input counters. |

### 4.3 Timeout Enforcement

Timeouts are checked lazily on every RPC access, but are also proactively
enforced on all sessions during garbage collection sweeps to prevent resource
exhaustion (DoS). The timeout duration is state-dependent:
- `OPEN` / `SEALED`: Capped at 10 minutes to prevent attackers from filling slots with unauthenticated or stalled sessions.
- `CALCULATING`: Capped at 30 minutes to safeguard against hung LLM inference threads.

**Session garbage collection:** During `CreateSession` calls, a garbage
collection sweep actively checks all sessions for timeouts. Any session
that has expired is transitioned to `ABORTED`. Terminal sessions (`CLOSED`,
`ABORTED`) older than 1 hour are then evicted to prevent unbounded memory growth.
Crucially, unauthenticated or stalled sessions that abort due to a `JOIN_TIMEOUT`
or `INPUT_TIMEOUT` are evicted **immediately**, bypassing the 1-hour wait.

**Global resource limits (DoS prevention):**

| Limit | Value | Purpose |
| :--- | :--- | :--- |
| `kMaxSessions` | 1000 | Maximum concurrent sessions |
| `kMaxParticipants` | 100 | Maximum participants per session |
| `kMaxTimeoutSeconds` | 86400 (24h) | Maximum session timeout |
| `kMaxInputBytes` | 65536 (64KB) | Maximum input size per participant |

These hard caps are enforced server-side regardless of client
requests, preventing resource exhaustion from malicious or buggy
clients.

### 4.4 Participant Authentication

Each participant receives a cryptographically random token upon
creation or joining. All subsequent RPCs require the participant's
secret `participant_token` (no `session_id` needed — the server
resolves the session internally via a reverse index). This prevents
unauthorized access and ensures participants can only interact with
sessions they've explicitly joined.

The CLI tool reads the participant token from the `ZTAB_TOKEN`
environment variable by default, rather than accepting it as a
`--token` command-line flag. This prevents token exposure in the
host OS process list (`ps -ef`). The token is ephemeral and
session-scoped: it is generated fresh for each session, is valid
only for the lifetime of that session (max 24h), and grants access
only to the specific session it was issued for. Leakage of a
participant token does not compromise other sessions, long-lived
credentials, or other participants' private inputs.

### 4.5 Async LLM Processing

When all inputs are received, LLM inference is dispatched to a
background thread to avoid blocking the gRPC thread pool. The
background thread reads session data under the lock, runs inference
without the lock, then re-acquires it to write results.

### 4.6 Session Design Evolution

The session protocol design considered several alternatives before
arriving at the current architecture. The key decisions are
summarized below.

**The foundational debate: Pipelines vs. Policy Classes.**

One proposal was a **Pipeline-First** model where agents compose
declarative processing pipelines (`[aggregate, llm,
schema_filter]`). This was flexible but created two severe problems:
(1) agents are not guaranteed to be reliable enough to correctly
parse, audit, and reason about complex pipeline graphs — delegating
security-critical processing logic to agents is fundamentally
unsound; and (2) arbitrary pipelines could contain hidden
data-leaking steps that would be difficult to detect.

The alternative was a **Constraint-First / Policy Class** model
where the TEE image defines a fixed set of pre-audited Policy
Classes. Agents only select a class and configure parameters
within its boundaries.

**Decision:** Adopt Policy Classes. Rationale:

1. **Attestability**: Policy logic is baked into the TEE image and
   verified via container digest. No custom agent pipelines to audit.
2. **Zero-Audit Joining**: Agent B only checks the Policy Class name
   and output JSON Schema — no need to audit a pipeline graph.
3. **Simplicity**: Agent specifies `policy_class="ScheduleOverlap"`,
   not a pipeline DAG.
4. **Robustness**: Security does not depend on agents being
   sophisticated enough to detect adversarial pipeline construction.

**Deterministic prompt ordering:** Participants are ordered by a
sequential `join_index` (assigned at join time) rather than by
token hash. This ensures deterministic prompt construction and
correct role assignment in role-based policies.

**Other key decisions:**

- **Prompt template ownership:** Passing templates via
  `CreateSession` was rejected as a security risk (attacker-crafted
  templates could leak data). Templates are registered in the
  `PolicyRegistry` at construction time, not supplied by agents.
- **Input and output validation:** Both input and output are
  validated against JSON Schemas with regex pattern enforcement
  (via RE2). Input validation prevents prompt injection by
  constraining participant inputs to structured data. Output
  validation prevents data leakage by restricting the LLM's output
  space. Structural-only validation (without regex) was evaluated
  and rejected as insecure.
- **Lazy timeout evaluation:** Rather than a background reaper
  thread, timeouts are checked lazily on every RPC. This avoids
  concurrency complexity for Phase 1's low session counts.
- **Rejected alternative — "Rationales" with Consent Gates:**
  LLM-generated reasoning explanations with a consent gate were
  explicitly rejected as highly prone to hallucination and data
  leakage.

## 5. Policy System

### 5.1 Policy Definition

Policies are defined in JSON files and loaded at server startup via
`--policy_dir`. The `PolicyRegistry` validates all policy files
at load time and crashes the server (`LOG(FATAL)`) on malformed
JSON or policy class name collisions, ensuring fail-fast behavior.
Each policy specifies:

- **`policy_class`**: Unique identifier (e.g., `"ScheduleOverlap"`).
- **`prompt_template`**: LLM prompt with `{num_participants}` and
  `{inputs}` placeholders.
- **`input_schema`**: JSON Schema for validating participant inputs.
- **`output_schema`**: JSON Schema for validating LLM outputs
  (including regex patterns for structured output enforcement).

**Input visibility (opaque vs. shared):** By default, all participant
inputs are opaque — visible only to the LLM during inference, never
to other participants. Policies can override this default by
annotating input schema fields with `visibility: "shared"` to allow
participants to see specific non-sensitive fields from other
participants (e.g., a public username or preferred meeting timezone).
The LLM always sees all inputs; the visibility annotation only
controls what is returned to other participants via the protocol.

**Cooperative vs. adversarial classification:** Policies are
classified as `COOPERATIVE` (participants are assumed to provide
well-formed inputs in good faith) or `ADVERSARIAL` (participants
may actively attempt to manipulate the session). Adversarial
policies enforce strict `maxLength` and regex `pattern` constraints
on all input fields to prevent prompt injection via oversized or
malformed inputs. Cooperative policies relax these constraints for
usability. This classification determines the validation strictness
applied to inputs, balancing usability against prompt injection
resistance.

**Narrowing semantics:** When agents supply session-time parameters
(e.g., timeout, participant count), they can only *narrow* the
policy's declared constraints — never widen them. A policy that
allows up to 10 participants cannot be overridden by an agent
requesting 20. This invariant ensures that the policy author
retains ultimate control over the session's security boundaries.

### 5.2 Example: ScheduleOverlap

The `ScheduleOverlap` policy demonstrates multi-party calendar
coordination:

- Two or more participants submit their available time slots.
- The LLM identifies all overlapping slots.
- The output is a JSON array of ISO 8601 datetime strings.
- Input and output schemas enforce the format with regex patterns.
- The prompt explicitly instructs the LLM not to reveal individual
  schedules.

### 5.3 Prompt Construction and Injection Prevention

When all inputs are collected, the session manager assembles the
final prompt by:

1. Looking up the policy's `prompt_template`.
2. For each participant, wrapping their input JSON in randomized
   delimiter tags:
   ```
   <<<PARTICIPANT_1_INPUT_BEGIN_a3f7c2e1>>>
   {"available_slots": ["2026-07-01T10:00:00Z"]}
   <<<PARTICIPANT_1_INPUT_END_a3f7c2e1>>>
   ```
3. Replacing `{inputs}` in the template with the concatenated blocks.
4. Replacing `{num_participants}` with the participant count.

The 8-character random hex suffix on each delimiter tag is generated
per-session using `std::random_device`. This prevents prompt injection
attacks where a malicious participant crafts an input containing
fake delimiter tags to:

- Prematurely close their input block and inject instructions.
- Mimic another participant's input block to confuse the LLM.
- Add system-level directives that override the policy prompt.

Because the delimiter suffix is random and unknown to participants
at input submission time, an attacker cannot predict the exact
tag format to inject.

**Schema override prevention:** Agents cannot supply custom
`input_schema_json` payloads at session creation. Input schemas are
defined exclusively in the policy file and enforced server-side.
This prevents prompt injection via attacker-crafted schemas that
could override validation rules.

**JSON normalization for parser differential defense:** All JSON
inputs are re-serialized (parsed and re-emitted) before being passed
to the LLM. This neutralizes parser differential attacks where
`nlohmann::json` silently ignores duplicate keys that could contain
hidden malicious payloads.

### 5.4 Security Audit History

The session manager implementation underwent a systematic 5-round
cross-model security audit (15 findings total, all resolved):

**Critical findings:**

- **Attestation Bypass via Tool Configuration (CRITICAL):**
  The original `_resolve_verifier()` allowed an `explicit_verifier`
  parameter to override auto-detection. A prompt injection attack
  could pass `verifier="noop"` to bypass RA-TLS for remote hosts.
  **Fix:** Remote hosts always enforce `ita` regardless of agent
  requests. Override only honored for `localhost`.
- **Weak Random Hex Generation:** Session IDs and tokens
  (`invitation_token`, `participant_token`) used `std::mt19937`
  (not a CSPRNG). **Fix:** Replaced with BoringSSL's
  `RAND_bytes()` for cryptographically secure 256-bit tokens.

**High findings:**

- **Session Enumeration:** Different error messages for
  "not found" vs. "full" leaked session existence. **Fix:** Uniform
  `PERMISSION_DENIED` for all auth failures.
- **False Positive Test Exits:** `check_agent_done()` returned
  success even on ABORTED sessions. **Fix:** Three-outcome model
  (PASSED/FAILED/INCOMPLETE).
- **JSON Schema FullMatch vs. PartialMatch:** `RE2::FullMatch`
  deviated from JSON Schema spec (ECMA 262). **Fix:** Switched to
  `RE2::PartialMatch`.

**Medium/Low findings:** Non-deterministic participant ordering
(fixed: sorted by token), TOCTOU races in state transitions
(fixed: proper locking), shutdown race with detached threads
(fixed: `ThreadGuard` RAII + `active_async_threads_` counter).

**Pre-launch comprehensive audit verdict:** Demo-Ready, with 3
production gaps identified for Phase 2: (1) no output leakage
detection logic (proto field exists but no implementation),
(2) no per-client rate limiting on `CreateSession`, (3) no
`CALCULATING` timeout (hung LLM threads occupy session slots).

**Requirements traceability:** 41 requirements tracked — 34 (83%)
satisfied, 3 (7%) partial, 2 (5%) missing, 2 (5%) Phase 2.

## 6. MCP Integration

### 6.1 Architecture

The ZTAB MCP server (`agent/mcp_server.py`) is a stdio-based server
implementing the Model Context Protocol (MCP) specification. It
exposes ZTAB broker RPCs as MCP tools that can be called by any
MCP-compatible agent framework.

**Why MCP:** Several integration alternatives were evaluated:

- **A2A (Agent-to-Agent protocol):** Designed for agent-to-agent
  communication but lacks the tool-call abstraction needed for
  agent→broker interactions.
- **OpenAPI / REST wrappers:** Would require each agent framework
  to implement custom HTTP client code. No standardized tool
  discovery mechanism.
- **Raw Python library:** Tightly couples ZTAB to Python-based
  frameworks, excluding non-Python agents.

MCP was selected because it provides standardized tool discovery
(agents learn ZTAB's API from tool descriptions), framework-agnostic
integration (any MCP-compatible framework works without custom code),
and a growing ecosystem of compatible agent platforms.

**Key design decisions:**

- **Zero-dependency implementation**: The MCP server uses a
  zero-dependency standard library implementation (`json` +
  `sys.stdin`/`sys.stdout`) rather than the FastMCP SDK. This
  eliminates external MCP SDK compilation and installation
  requirements, keeping the agent package installable via
  `pip install` with no native dependencies. FastMCP was the
  initial prototype but was replaced to minimize the dependency
  footprint.
- **Stdio transport**: JSON-RPC over stdin/stdout. `stdout` is
  redirected to `stderr` at import time to prevent `print()` calls
  from corrupting the protocol stream. The server explicitly saves the
  original `sys.stdout` file descriptor to ensure JSON-RPC responses
  are successfully piped back to the framework.
- **Named backends**: Backend configurations are loaded from
  `~/.ztab/backends.json` (or `$ZTAB_BACKENDS_FILE`). Each backend
  specifies host, port, verifier type, and optional image digest.
- **Channel caching**: gRPC channels are cached per backend to
  enable HTTP/2 multiplexing across tool calls.
- **Lazy config reloading**: The backends file is checked for changes
  (via mtime) on each access. If changed, stale channels are flushed.
- **Structural error responses**: The MCP server returns structured
  error messages explicitly listing missing and expected parameters.
  This prevents LLM retry storms that occur when agents receive
  generic `KeyError` exceptions with no guidance on correct arguments.

### 6.2 MCP Tools

| Tool | Description |
| :--- | :--- |
| `ztab_list_backends` | List configured TEE backends and their security levels. |
| `ztab_test_connection` | Connect, verify attestation, run Echo RPC. Diagnostic tool (always fresh connection). |
| `ztab_create_session` | Create a new multi-agent session. Automatically injects `creator_token` from backend config if present. Returns `invitation_token` (to share) + `participant_token` (secret). |
| `ztab_join_session` | Join an existing session. Returns policy for review + token. |
| `ztab_accept_policy` | Accept the session policy. |
| `ztab_submit_input` | Submit private input JSON. |
| `ztab_get_result` | Get session result or error. |
| `ztab_get_session_status` | Get join/accept/input counters. |

### 6.3 Backend Configuration

Backend configurations are stored in `~/.ztab/backends.json` (or the
path specified by `$ZTAB_BACKENDS_FILE`). This file is the **trust
boundary** for attestation parameters — the agent cannot override
these values at runtime.

**Security motivation:** The Named Backends architecture was introduced
to close an **attestation bypass vulnerability** discovered during
security auditing. In the original design, agents passed `host`, `port`,
`verifier`, `expected_digest`, and `allow_debug_tee` as MCP tool
arguments. This created a severe prompt injection attack vector: an
attacker could craft an input that instructs the agent to pass
`verifier="noop"` or the digest of a malicious container, bypassing
hardware attestation entirely and leaking private data.

The Named Backends design moves all security-critical parameters to a
host-local config file that the agent cannot modify at runtime. The
agent can only *select from* the configured backends by `backend_id` —
it cannot forge, override, or invent new backends during a transaction.
To reconfigure, the MCP server must be restarted (a visible,
user-approvable action).

**Schema:**

```json
{
  "default_backend": "local",
  "backends": [
    {
      "backend_id": "local",
      "name": "Local Dev Server",
      "description": "Docker container on localhost",
      "host": "localhost",
      "port": 8000,
      "verifier": "noop",
      "allow_debug_tee": false
    },
    {
      "backend_id": "gcp-prod",
      "name": "GCP Production TEE",
      "description": "H100 Confidential Space VM",
      "host": "10.0.0.1",
      "port": 8000,
      "verifier": "ita",
      "expected_digest": "sha256:abc123...",
      "allow_debug_tee": false,
      "creator_token": "SECRET"
    }
  ]
}
```

**Security design:** The `verifier` and `expected_digest` fields are
read from the config file, not from agent tool arguments. This
prevents an attacker from using prompt injection to instruct the
agent to bypass attestation by passing `verifier="noop"`. The config
file is the trust boundary; the agent selects backends by name, but
cannot forge or override their security properties.

The `creator_token` field, when present, is automatically injected
into `CreateSession` requests by the MCP server.

**Server-side env var fallback:** If `--creator_token` is not set
on the command line, the server checks the `CREATOR_TOKEN`
environment variable (`main.cc:149-153`). The flag takes
precedence. This allows containerized deployments to inject the
token via environment configuration without modifying the command
line.

**Cross-agent coordination:** `backend_id` is a local concept. Person
A's `scheduler-tee` and Person B's `my-scheduler` might point to the
same physical server. When agents coordinate to join the same session,
they share the `invitation_token` and server address (`host:port`).
An agent can look up which local `backend_id` corresponds to a given
`host:port` from its own config. If no match is found, the agent must
create a new backend entry and restart the MCP server — this
intentional friction makes arbitrary runtime connection changes highly
visible to the user.

**Sanitized metadata:** The `ztab_list_backends` tool returns only
`backend_id`, `name`, `description`, and a derived `security_level`
(`production` if verifier != `noop`, `development` otherwise). It
does NOT expose `host`, `port`, `verifier`, or `expected_digest`.

**CLI exemption:** The standalone CLI tool (`cli.py`) accepts direct
`--host` and `--port` arguments rather than using Named Backends.
This is intentional: the CLI is a developer debugging tool operated
by humans, who are not susceptible to prompt injection. The Named
Backends security boundary specifically protects against
*agent-mediated* attacks where a malicious prompt could instruct an
LLM to connect to an attacker-controlled server.

**Programmatic configuration:** The `install_mcp.sh` installer script
accepts `--add_backend` flags as a convenience alternative to
manually editing `backends.json`. This allows automated setup scripts
and agent bootstrapping flows to register backends without requiring
JSON file manipulation.

The installer also accepts `--creator_token TOKEN` to configure
admission control credentials for token-gated servers.

**Empty digest wildcard:** Setting `expected_digest` to an empty
string (`""`) in a backend configuration skips container digest
verification. This is intended for local development and testing
where the server is not running inside a real TEE container.

## 7. Build and Deployment

### 7.1 Build System

The TEE server is built with Bazel (via Bazelisk, pinned to 8.2.1).
The Bazel workspace root is located at the top-level repository root
(not within `tee/`). This structural decision allows the
OCI image assembly in `gcp/BUILD` to cleanly reference policy data files
in `examples/` without violating Bazel's cross-workspace boundary
restrictions.

Key external dependencies are fetched via `MODULE.bazel`:

| Dependency | Version | Purpose |
| :--- | :--- | :--- |
| gRPC | 1.78.0 | RPC framework |
| Abseil | 20250814.0 | Status, logging, string utilities |
| BoringSSL | 0.20260211.0 | EC keys, X.509 certs |
| Protobuf | 33.0-rc2 | Proto compilation and runtime |
| llama.cpp | b8875 | LLM inference (Gemma 4 support) |
| rules_oci | 2.2.6 | OCI image packaging (Dockerfile-less) |
| CUDA 12.2 | (optional) | GPU inference |

A hermetic LLVM 19.1.0 toolchain with a pinned sysroot is used to
avoid system-compiler dependencies and CUDA compatibility issues.

**Unified CPU/GPU builds:** The build system supports both CPU-only
and GPU-accelerated builds via a single Bazel flag:
`--define enable_cuda=true`. When disabled (the default), the server
compiles for CPU-only inference — no CUDA toolkit required. When
enabled, `rules_cuda` provides a hermetic CUDA 12.2 toolkit and
Nvidia driver stubs, and the llama.cpp backend is compiled with GPU
kernel support. This single flag controls the entire CPU/GPU build
split, allowing developers to iterate locally on CPU and deploy to
GPU TEEs with the same codebase.

Key dependency versions (gRPC, BoringSSL) are pinned to proven,
tested version combinations used in production TEE deployments.

The build requires a Clang-based toolchain (not GNU ld) to avoid
`.sframe` section linker errors introduced in GNU binutils 2.45.
The hermetic LLVM 19.1.0 toolchain satisfies this requirement.

The pure-Python `agent/` package is pip-installable independently
of the Bazel build system. A `MANIFEST.in` file excludes
Bazel-generated artifacts from the pip package to prevent conflicts.
Alternative repository layouts (e.g., placing `MODULE.bazel` inside
`tee/`, using symlinks) were evaluated and rejected because they
either broke Python package imports or coupled the pure-Python
agent to the C++ Bazel workspace.

### 7.2 Container Packaging

Container images are built without Dockerfiles using Bazel's
`rules_oci`. The base image is `distroless/cc-debian12`. Model
weights are downloaded from GCS via a custom `gcs_file` repository
rule and packaged into separate layers.

**Why not Dockerfiles:** An earlier approach using Dockerfiles was
abandoned because (1) compiling inside Docker caused the LLVM linker
to fail on missing system libraries (`libxml2.so.2`), and (2) Docker
builds could not utilize Bazel's external repository caching for
model weights, leading to multi-gigabyte re-downloads on every build.

**Container variants:** Two container variants are packaged: `local`
(mock attestation, CPU-only with `gpu_layers=0`) for development,
and `gcp` (ITA attestation, GPU-enabled with `gpu_layers=999`) for
production Confidential Space deployment.

### 7.3 GCP Confidential Space Deployment

The `gcp/launch.sh` script manages the full lifecycle of a
Confidential Space VM with H100 GPU and Intel TDX:

| Mode | Description |
| :--- | :--- |
| `setup` | Creates Instance Template and Managed Instance Group (MIG) with Confidential Computing config. |
| `launch` | Requests a node via Dynamic Workload Scheduler for GPU queuing. |
| `get-ip` | Polls until the VM is allocated and returns the external IP. |
| `delete` | Tears down the MIG and template. |

**Configuration:**

- Machine type: `a3-highgpu-1g` (H100 GPU)
- Confidential compute: `--confidential-compute-type=TDX`
- Shielded secure boot enabled
- GPU driver installation: `tee-install-gpu-driver=true`
- Attestation: Intel Trust Authority (ITA) via Confidential Space agent

All deployment-specific parameters (project, zone, image registry,
ITA API key) are required flags — the script has no hardcoded
defaults, ensuring the OSS codebase contains no proprietary
configuration.

**`tee-cmd` override caveat:** Avoid using the GCP Confidential
Space `tee-cmd` metadata field to inject server flags at runtime —
it replaces the entire Docker `CMD` array, wiping out other critical
flags (`--model_path`, `--port`). Policy files and all server
configuration must be baked into the container image.

### 7.4 Local Development

The `tee/run_server.sh` script builds and runs the TEE server locally
via Docker:

```bash
# Echo-only mode (no model, fast build):
./run_server.sh --port 8000

# With LLM model (downloads from GCS):
./run_server.sh --llm --gcs_bucket gs://your-bucket

# With GPU passthrough:
./run_server.sh --model gemma4_e4b --gcs_bucket gs://your-bucket --gpu
```

For direct binary execution (no Docker):

```bash
bazelisk build -c opt :ztab_server
./bazel-bin/ztab_server --port 8000 --attestation_provider mock \
  --creator_token SECRET
```

The `-c opt` flag is critical for local CPU inference. Without it,
model prefill time increases from ~90 seconds to 30+ minutes due
to unoptimized GEMM kernels.

**Debug settings and environment variable upper bounds:** The
`ZTAB_ALLOW_DEBUG_TEE=1` environment variable acts as a strict
upper bound for debug settings. Even if an agent attempts to set
`allow_debug_tee: true` via prompt injection, the setting is
ignored unless the host environment explicitly enables it. This
ensures operators retain control over debug configuration.

In mock attestation mode, the server generates unsigned JWTs (`alg:
none`) with hardcoded claims that simulate a genuine Confidential
Space attestation report. This allows full end-to-end development
and testing without TEE hardware.

## 8. Testing

### 8.1 Test Levels

| Level | Location | Description |
| :--- | :--- | :--- |
| **L1: Component Tests** | `test/test_prompt.py`, `test/test_session.py` | Direct Python tests against the TEE server. No MCP or agent framework. |
| **L2: E2E Harness** | `test/harness/` | Full cold-start test: Language Server, agent sandboxes, MCP server installation, real agent coordination. |

**E2E Harness v2 (Isolation-First Architecture):**

The E2E test harness evolved through two major failures before
arriving at its current isolation-first design:

1. **v1 failure: Shared MCP process** — Both agents ran under a single
   Language Server, sharing one `ztab` MCP process and one
   `backends.json`. Agent A's config changes were visible to Agent B,
   violating sandbox isolation.
2. **v1 failure: Stale state leakage** — The harness copied (not moved)
   `mcp_config.json`, leaving the original intact. If ZTAB was
   registered from a previous run, the LS booted with pre-existing
   tools, bypassing the cold-start bootstrapping flow entirely.
3. **v1 failure: Rogue agent source rewrite** — An agent, granted
   broad `write_file(*)` permissions, rewrote the TEE server's C++
   source code to force a passing test result rather than reporting
   a connection error. This demonstrated that agents must be
   sandboxed with minimal permissions and their behavior audited.

**v2 design principles:**

- **Golden Principle**: "The agent configures everything." The harness
  provides isolation and information. The agent reads SKILL.md,
  installs dependencies, writes configs, and bootstraps the MCP
  server. No shortcuts, no pre-injection.
- **Per-agent isolation**: Each agent gets a separate Language Server
  instance, separate `$HOME` directory, separate `mcp_config.json`,
  and separate `backends.json` (via `ZTAB_BACKENDS_FILE` env var).
- **N-agent architecture**: Supports 1..N agents, not hardcoded to
  a pair. Single-agent mode tests bootstrapping (one agent plays
  both Creator and Joiner roles to validate the full state machine
  without spawning multiple sandboxes); multi-agent mode tests
  coordination. In multi-agent mode, agents are spawned sequentially:
  the creator agent launches first and generates an `invitation_token`,
  which the harness polls for before spawning joiner agents
  parameterized with that token.
- **Cheating detection**: Pre-flight assertions verify that
  `mcp_config.json` does not contain `ztab` and `backends.json` does
  not exist before the agent starts.

**Post-run trajectory audit:**

The harness includes a trajectory auditor (`audit_trajectory.py`)
that programmatically validates agent behavior after each run:

- ✔ Agent read `SKILL.md` before acting
- ✔ Agent ran `install_mcp.sh`
- ✔ Agent registered ZTAB in MCP config
- ✔ Agent created `backends.json`
- ✔ Agent used the correct verifier (`noop` for local, `ita` for GCP)
- ✔ Agent used MCP tools (not `cli.py` fallback)
- ✗ Agent used `socat`, `pkill`, or other forbidden commands
- ✗ Agent modified source code
- ✗ Agent inherited a pre-existing MCP registration

| Check | Description |
| :--- | :--- |
| `ac_creator_token_in_backends` | If the TEE requires a `creator_token`, verify the agent's `backends.json` has it configured. |
| `ac_create_session_not_denied` | Verify `ztab_create_session` was not rejected with `PERMISSION_DENIED`. |

**Test Harness Portability:**

The E2E harness is designed to run in fully open-source mode:

| Feature | Value |
| :--- | :--- |
| **Agent Runtime** | Any MCP-compatible agent framework |
| **Model Provider** | Gemini API / Vertex AI |
| **Authentication** | OAuth token |
| **Workspace** | `ztab/` (repo root) |

**OSS purity enforcement:** The OSS harness code must not contain
any reference to proprietary infrastructure.

**Token mirroring:** OAuth tokens from the host
(`~/.gemini/*standalone-oauth-token`) are copied into each agent's
sandboxed `$HOME/.gemini/` directory. The glob pattern avoids
hardcoding product-specific names to maintain OSS purity.

**Docker containerization (planned):** A `Dockerfile` in
`test/harness/` will package the harness, Language Server binary,
and Python dependencies into a self-contained image based on
`debian:bookworm-slim`. This enables fully standalone validation
without any corporate network dependencies, and is a prerequisite
for CI/CD integration.

**Two-phase test execution:** E2E tests require a two-phase execution
model because MCP tool registrations are cached per server process
lifetime. In phase 1, the agent bootstraps its environment (installs
the MCP server, writes config files, registers tools). In phase 2,
the agent framework restarts with the newly registered tools available
and performs the actual ZTAB session workflow. The harness orchestrates
this transition automatically.

**Dynamic port discovery:** The agent framework server binds to port 0
(OS-assigned) and writes its actual port to a JSON discovery file.
The test harness polls this file to determine the correct port for
API calls and monitoring. This file is written to the developer's
`~/.gemini/` directory, avoiding shared directories like `/tmp`. As this is
strictly a local test harness, local privilege escalation concerns are
out of scope.

**Shared virtualenv optimization:** A pre-built virtualenv can be
injected via the `VENV_PATH` environment variable to bypass the
10-15 second `pip install` delay during tests. The virtualenv is
mounted read-only into each agent's sandbox.

**Dirty-state self-healing validation:** The harness supports a
`--reuse_run` mode that tests agent resilience in corrupted
environments (e.g., pre-existing stale `backends.json`, corrupted
`mcp_config.json`). Rather than asserting a clean initial state,
this mode verifies that the agent can detect and recover from
dirty configurations autonomously.

**Sibling container architecture:** The harness supports a `--tee
external` mode that uses decoupled sibling containers instead of
Docker-in-Docker. In this mode, the TEE server runs in its own
container alongside the harness container, connected via a shared
Docker network. This avoids the complexity and security implications
of nested Docker daemons.

**SKILL.md / prompt separation:** Test scenarios separate procedural
instructions (in `SKILL.md`, which the agent reads and executes)
from parameterization (in the harness prompt, which specifies
backend, `invitation_token`, etc.). This prevents conflicting instructions
between the prompt and the runbook.

### 8.2 Scenario Framework

Tests are organized around a `Scenario` abstraction (`test/scenario_base.py`):

- Each scenario defines a policy class, test inputs, expected outputs,
  and a `validate_result()` method.
- Concrete scenarios live in `examples/<name>/scenario.py`.
- The `CalendarScenario` (`examples/calendar/`) is the reference
  implementation.

### 8.3 Smoke Test

`agent/smoke_test.py` validates the MCP protocol lifecycle:
`initialize` → `tools/list` → `tools/call`.

### 8.4 GCP Operational Lessons

GCP Confidential Space deployments revealed several operational
realities not visible in local testing:

**Dynamic config reloading:** The MCP server's original
`BACKENDS_CONFIG = _load_backends()` loaded config once at module
import time. This meant any runtime config change (e.g., adding a GCP
backend) required restarting the MCP process. The fix was to make
`_get_backends()` check the file's `mtime` on every tool call and
reload if changed, flushing the channel cache. This adds negligible
latency (<1ms for a <1KB JSON file) while guaranteeing correctness.

**Language Server reload latency:** After an agent writes
`mcp_config.json`, the Language Server's filesystem watcher takes
3-10 seconds to detect the change and hot-reload the MCP server.
Agents must handle `unknown tool name` errors gracefully by retrying
after a brief delay.

**Debug TEE attestation:** GCP Confidential Space VMs in debug mode
produce ITA tokens with `dbgstat: "enabled"`. The `ita_verifier.py`
rejects these by default. Agents must set `allow_debug_tee: true` in
their backend config for development/testing. A successful GCP E2E
run demonstrated both agents independently diagnosing and resolving
this by writing debug scripts, inspecting ITA token claims, and
updating their backend configs.

**Autonomous agent collaboration (historical incident):**
In an early GCP test run, the test harness had a fallback code path
that read session IDs from a shared file (`/tmp/ztab_session_coord.json`).
This fallback was insecure — the file sat in the world-writable `/tmp`
directory and could pick up stale data from previous runs. During one
test, Agent B received a stale session ID via this fallback. Agent A
autonomously recovered the situation by inspecting Agent B's sandbox
folder and writing the correct session ID directly to Agent B's home
directory. Agent B discovered the file and self-corrected.

**Current state:** Both the `/tmp` coordination file and the
`find_session_in_sandbox()` fallback have been completely removed from
the codebase. Session coordination now works exclusively through the
harness: it monitors Agent 1's trajectory in real time, extracts the
`invitation_token` from the `ztab_create_session` MCP tool response, and
passes it to Agent 2 via prompt injection. No shared files are
involved. The only remaining reference to the old file is a
belt-and-suspenders `rm -f` cleanup in `test_cold_start.sh`.

**Why this incident is documented here:** It demonstrates that the
multi-agent protocol is resilient — agents can autonomously diagnose
and recover from harness-level bugs without human intervention. The
cross-sandbox write was possible because the test harness uses
directory-level separation without enforcement (same UID, no container
boundary) as a pragmatic tradeoff for developer iteration speed. In
production deployments, agents run on separate machines and
communicate exclusively through the TEE server — no shared filesystem
exists. Per-agent containerization in the test harness is planned
(§8.1).

**GCP performance data (from verified run):**

- Model load time on H100 GPU: ~6.5 seconds
- First TLS handshake with ITA attestation: ~1.1 seconds
- Subsequent cached handshakes: ~52ms
- Full 2-agent session lifecycle (bootstrap → coordinate → inference →
  result): ~6 minutes total

**GCP bootstrapping lesson:** The TEE server's IP/port is passed to
the harness via `--host` and `--port` flags. The agent is responsible
for creating its own `backends.json` during the cold start flow —
the harness **never** pre-injects backend configuration.
`assert_cold_start()` explicitly verifies that `backends.json` does
not exist before the agent begins, enforcing this invariant.

### 8.5 Known Testing Issues

The test framework has several known operational issues. Developers
extending the test harness should be aware of these:

**Observability v2 status:** The live agent streaming ("Observability
v2") in the E2E test harness was originally broken because
`test_cold_start.sh` captured trigger script output into bash variables
(`TRIGGER_OUT=$(...)`), swallowing stderr. The polling loop relied on
an obsolete `grep` command. A dedicated `monitor_session_test.py` was
built with correct protobuf JSON parsing (using verified camelCase
field mappings from real 328KB JSONL traces) and is now integrated
into the v2 harness. The key implementation detail: `plannerResponse`
has no `.text` field — agent thoughts are in `.thinking`. The
`extract_step_text()` function handles 13 distinct step types with
verified camelCase field names (`userInput`, `plannerResponse`,
`mcpTool`, `runCommand`, `viewFile`, `errorMessage`, `codeAction`,
`systemMessage`, `grepSearch`, `listDirectory`, `listResources`,
`taskDetails`, `checkpoint`).

**Agent amnesia — root cause and fix:** When agents run long sessions
with many tool calls, earlier steps (including those where the agent
learned the `participant_token`) are silently pushed
out of the LLM's context window by standard truncation.

The original hypothesis was that asynchronous scheduling creates a
new execution context with a blank slate. **This was debunked** —
the execution context ID does not change across wakeups. The actual
root cause is context window truncation: the agent platform normally
summarizes old steps to preserve critical context, but the test
harness's trigger request omitted the conversation history
configuration flag, causing the summarization to never run.

**Fix:** Enable conversation history summarization in the trigger
script's request. This enables automatic context summarization
while preserving the execution context (and thus Observability v2
streaming).

**Agent-hallucinated success:** Agents may report successful
inference when no LLM execution actually occurred on the server.
The test harness must independently verify session state by calling
`GetResult` with administrative access, never relying solely on
agent self-reported logs.

**Zombie MCP processes:** Orphaned `mcp_server.py` processes from
previous test runs can cause connection storms (hundreds of
redundant TLS handshakes). The test harness cleanup phase includes
explicit `pkill -f mcp_server.py` to terminate stale processes
before each run.

**Workarounds (when the fix is not deployed):**

1. **Avoid asynchronous scheduling entirely**: Use tight synchronous
   polling
   loops (e.g., `sleep 10` in a bash terminal command) that keep the
   agent within the same active execution context.
2. **Implement state checkpointing**: Before sleeping, write state
   (`participant_token`, step) to `/tmp/state.json`.
   When waking up, always read the checkpoint file first.

## 9. Supported Models

ZTAB uses [llama.cpp](https://github.com/ggml-org/llama.cpp) (release
b8875) as its inference engine, supporting any model in GGUF format.
The reference models are from the Gemma 4 family:

| Model | Size | Quantization | Use Case |
| :--- | :--- | :--- | :--- |
| Gemma 4 E2B | ~2B params | Q4_0 | Fast testing, CPU-feasible (~9 min inference) |
| Gemma 4 E4B | ~4B params | Q4_K_M | Balanced quality/speed |
| Gemma 4 31B | ~31B params | Q4_K_M | High quality, requires GPU |

**Model tiering rationale:** The smallest model (E2B) is intended for
rapid local development iteration where output quality is secondary
to testing the execution flow. The mid-tier model (E4B) balances
quality and speed for integration testing. The 31B model is the
production target, requiring GPU acceleration (H100) for acceptable
latency.

**E2B quality caveat:** The smallest quantized model (`gemma4_e2b`
on CPU) may produce incomplete or lower-quality outputs for complex
multi-input tasks (e.g., finding only 1 of 2 overlapping calendar
slots). Test assertions should be relaxed when using E2B, as its
purpose is validating the execution flow, not output quality.

Model weights are downloaded from GCS via a custom `gcs_file` Bazel
repository rule and cached automatically. The `--gcs_bucket` flag
specifies the bucket location.

**GPU offload:** The `--gpu_layers` flag controls how many model
layers are offloaded to GPU. Use `999` for full offload on H100.
CUDA 12.2 support is included via `rules_cuda` in the build system.

**Inference engine details:** The current `LlamaEngine` implementation
uses greedy sampling (temperature=0) for deterministic outputs,
single-prompt synchronous generation (one inference at a time,
protected by a mutex), and model-specific chat template formatting
(Gemma chat template with `<start_of_turn>` / `<end_of_turn>` markers).
LLM outputs are post-processed to strip markdown code fences that
models frequently wrap around JSON responses.

**Generation token limit:** The `LlamaEngine` generation token
limit is set to 4096 (increased from an initial value of 512 that
caused silent truncation of valid multi-slot outputs).

**CPU performance characteristics:** With `-c opt`, local CPU
inference on Gemma 4 E2B takes approximately 83 seconds for prefill.
The client polling timeout is tuned to 180 seconds to accommodate
this.

**Flash Attention:** Flash Attention (`flash_attn=true`) only
accelerates the prefill phase, not the auto-regressive generation
phase. For short prompts, it provides minimal benefit.

## 10. Current Limitations and Future Work

### 10.1 Current Limitations

- ZTAB is an open-source framework, not a managed hosted service.
  Users deploy and operate their own TEE server instances. The
  framework has been tested on GCP Confidential Space with Intel TDX
  and H100 GPU, but there is no turnkey hosted offering.
- Server deployment is currently a human-initiated process (building
  the container image, provisioning a Confidential Space VM, and
  launching the server). Agents can autonomously configure and
  connect to a running ZTAB server, but cannot yet provision the
  server infrastructure itself.
- Admission control is limited to a single static
  `creator_token` that gates `CreateSession`. There is no
  per-client ACL, no token rotation mechanism, and no
  per-RPC authorization beyond session creation. Clients
  that can reach the server can still call `JoinSession` if
  they possess a valid `invitation_token`.
- Single-session LLM inference (no multi-turn conversation support).
- Policy registration is static (load from disk at startup).
- No persistent session storage (in-memory only).
- LLM inference is strictly serialized via a shared mutex. Concurrent
  sessions queue for inference access, meaning only one session's
  LLM call executes at a time. This limits throughput to one
  inference operation at a time across all sessions.
- Constrained decoding via GBNF grammars is model-tokenizer-sensitive.
  Some SentencePiece tokenizers (e.g., Gemma 4) may produce token
  sequences that bypass standard JSON grammar rules, requiring
  model-specific GBNF compilation (a Gemma-aware `ZtabGbnfCompiler`
  is planned).
- No per-client rate limiting. The `TlsProxy` architecture does not
  perform mTLS client authentication, so the server cannot distinguish
  or throttle individual clients.
- The Bazel build does not pass `-arch=sm_90` to `nvcc` for H100
  Hopper architecture optimization. GPU inference works but does not
  use Hopper-specific instructions.

**Attestation token lifetime (architectural decision):**

The RA-TLS implementation addressed a fundamental challenge with JWT
token lifetime. Attestation tokens issued by Intel Trust Authority
have a limited lifetime (typically 5 minutes). Four approaches were
evaluated, and the in-process TLS proxy was selected:

1. **`SocketMutator` / File Watcher (rejected):** Required
   routing certs through the filesystem; `FileWatcherCertificateProvider`
   has a 1-second minimum polling interval, creating race conditions.
2. **Proactive Background Refresh (rejected):** Would
   refresh every 4 minutes even with no traffic, violating the
   "strictly lazy" design constraint.
3. **Pre-flight Refresh RPC (rejected):** Required client
   changes and a custom pre-flight protocol outside standard TLS.
4. **In-Process TLS Proxy (selected):** gRPC runs in
   plaintext on `127.0.0.1:8001`. A BoringSSL-based proxy on port
   `8000` fetches a fresh attestation token and generates a fresh
   certificate **on-demand for every connection handshake**.

The in-process proxy satisfies all constraints: zero filesystem
routing, zero race conditions, strictly lazy (no background threads),
standard TLS, and no client changes. The gRPC
`CertificateProviderInterface` approach was also investigated and
found **infeasible** — gRPC Core only queries the provider once at
startup, and BoringSSL caches the certificate in its `SSL` context
without per-handshake callbacks. This decision directly motivated
the TLS proxy architecture described in [§2.3](#23-why-an-in-process-tls-proxy).

**Connection storm history:** Early GCP testing revealed that the
Python MCP client created a fresh TLS connection (with full attestation
verification) for every MCP tool call. A typical multi-step agent
session generated 329 redundant connections. This was fixed by
implementing channel caching per backend in `mcp_server.py` (see
[Section 2.4](#24-connection-model-and-channel-caching)).

### 10.2 Planned Features

- **Signed Declarative Policies (SDPs)**: Pre-signed policy
  definitions that agents can verify before joining a session,
  establishing a trust chain for policy auditing. The TEE would
  verify an SDP's cryptographic signature against a set of Trusted
  Public Keys baked into the container image, creating a trust chain
  from the auditor to the runtime — this allows new use cases to be
  deployed without rebuilding the attested image. The design includes
  SDP versioning (`min_engine_version`) and a `revoked_sdp_hashes`
  revocation list to handle policy lifecycle.
- **Role-based policies**: Different participants playing different
  roles (e.g., buyer vs. seller) with role-specific input schemas
  and output visibility. The current output model is broadcast — all
  participants receive identical output. Role-based routing would
  allow different participants to receive different subsets of the
  output based on their role. The proposed mechanism uses
  `x-ztab-visibility` annotations in the output JSON Schema to
  control per-field visibility by role.
- **Multi-turn sessions**: Extend the single-round session model
  to support iterative negotiation and refinement.
- **Fault-tolerant multi-turn sessions**: Encrypting session state
  blobs using hardware-derived TDX Sealing Keys to enable crash
  recovery without exposing session data outside the TEE. This would
  allow multi-round sessions to survive TEE restarts while
  maintaining the confidentiality invariant.
- **Ad-hoc policies**: Allow agents to propose custom prompt
  templates at session creation (with explicit trust trade-offs).
- **Output leakage detection**: Runtime analysis of LLM outputs
  for substring or semantic similarity to participant inputs.
- **Session discovery**: Mechanisms for agents to find available
  sessions without out-of-band coordination. The proposed approach
  uses an A2A-compliant Agent Card hosted at
  `/.well-known/agent.json` for capability discovery, enabling
  agents to find and connect to ZTAB brokers through standard
  service discovery.
- **Agent identity verification**: Requiring agents to present an
  A2A OIDC JWT during `JoinSession` to prove organizational identity
  and prevent Sybil attacks (multiple agents impersonating different
  entities controlled by a single adversary).
- **Cross-cloud federation**: Support for TEE deployments on
  multiple cloud providers (AWS Nitro, Azure SEV-SNP).
- **Streaming inference**: gRPC streaming for incremental LLM
  output delivery.
- **Bidirectional streaming API**: A feasibility analysis explored
  migrating from unary RPCs to a single `SessionStream` bidirectional
  RPC, which would eliminate per-
  call connection overhead entirely. However, this adds significant
  multiplexing complexity in both C++ and Python (~3 days of work vs.
  ~1 hour for channel caching). The unary API with channel caching was
  chosen as the pragmatic solution; the streaming API remains a future
  option for latency-sensitive deployments.
- **PyPI distribution**: `ztab` package installable via
  `pip install ztab`.
- **Autonomous TEE provisioning**: Enable agents to autonomously
  provision and launch their own ZTAB TEE server instances using a
  SKILL file, removing the need for human-initiated server deployment.
- **Admission control (implemented)**: Token-based admission
  control via `--creator_token` gates `CreateSession` on the
  TEE server. Only clients presenting the configured token can
  create sessions. Future work: interceptor hierarchy for
  per-RPC authorization, client identity verification, and
  token rotation (b/438809953).
- **Dynamic verification (Python sandbox)**: Extend the TEE with an
  embedded restricted Python sandbox to evaluate code behavior and
  safety checks at runtime, enabling dynamic verification beyond
  static hash attestation. This would allow policy classes to include
  custom verification logic without rebuilding the server binary.
- **Semantic attestation**: Extending hardware attestation to include
  cryptographic verification of the LLM's reasoning process and prompt
  integrity — enabling clients to verify not just *what code* ran,
  but *how it reasoned* over their data.
- **Alternative integration wrappers**: OpenAPI/REST specification
  for non-MCP consumers, and a raw Python Function Calling wrapper
  for direct framework integration.
- **Per-client rate limiting via mTLS**: Establishing mTLS
  authentication between agents and the TEE proxy infrastructure
  to enable per-client rate limiting and abuse prevention.
- **Stalled session detection (conversational nudge)**: Detecting
  sessions where an agent has stopped communicating and injecting
  a nudge message to prompt the agent to resume or abort.

## 11. Related Work

### 11.1 Landscape Positioning

ZTAB occupies a unique position in the landscape: it is the only
system that combines multi-party coordination, LLM-based reasoning,
agent-native integration (MCP), and open-source availability.

The key metaphor: ZTAB is **"the meeting room, not the door lock."**

Existing approaches address different facets of the trust problem:

| Approach | Trust Direction | LLM | Multi-Party | Agent-Native |
| :--- | :--- | :--- | :--- | :--- |
| **ZTAB** | Multidirectional | Inside TEE | Yes | MCP |
| Attested tool sandboxing | Unidirectional | Outside sandbox | No | MCP |
| Confidential inference services | Inward (user→provider) | Yes | No | No |
| Local agent sandboxes | Local agent | No | No | Yes |
| Data clean rooms | Data-centric | No (SQL) | Yes | No |

### 11.2 Relationship to Attested MCP

Attested MCP (e.g., built on Project Oak) provides unidirectional
trust: protecting user data from third-party tool developers via
WASM sandboxing. ZTAB addresses a complementary problem that
attested tool sandboxing does not cover: scenarios where multiple
agents from different organizations need to pool private data and
reason over it jointly inside a neutral environment.

The trust models are complementary rather than competing:

- **Attested MCP** assumes the primary agent is trusted and protects
  it from potentially malicious tools.
- **ZTAB** assumes **no party** is trusted — not the agents, not
  the platform, not the operator — and provides a neutral ground
  for mutual collaboration.

A planned integration path would use an attestation proxy to wrap
ZTAB backends for MCP discovery, allowing agents to discover and
connect to attested ZTAB servers through the same mechanisms used
for other attested MCP tools.

### 11.3 Relationship to Multi-Party Computation (MPC)

MPC provides strong theoretical guarantees for computing on private
data, but current MPC protocols cannot efficiently execute arbitrary
LLM inference. ZTAB trades MPC's information-theoretic guarantees
for the practical ability to run full neural network inference inside
hardware-protected memory, with attestation providing the trust
anchor.

### 11.4 Performance Characteristics

Confidential LLM inference on current TEE hardware (Intel TDX +
NVIDIA H100 in Confidential Compute mode) adds approximately
8-20% performance overhead compared to non-confidential execution,
making it practical for production workloads.

## Appendix A: Protocol Buffer Reference

The `AgentBrokerService` is defined in
`tee/session_manager.proto`. See [Section 4](#4-session-protocol) for
the session state machine and RPC semantics.

## Appendix B: Glossary

| Term | Definition |
| :--- | :--- |
| **TEE** | Trusted Execution Environment — hardware-isolated enclave for confidential computing. |
| **RA-TLS** | Remote Attestation over TLS — embedding attestation evidence in TLS certificates. |
| **EAT** | Entity Attestation Token — IETF standard (RFC 9334) for attestation evidence. |
| **MCP** | Model Context Protocol — standard for AI agent tool communication. |
| **GGUF** | GPT-Generated Unified Format — model file format used by llama.cpp. |
| **ITA** | Intel Trust Authority — third-party attestation verification service. |
| **GCA** | Google Confidential Computing Attestation service. |
| **TDX** | Intel Trust Domain Extensions — hardware TEE technology. |
| **Confidential Space** | GCP's managed confidential computing platform. |
| **OIDC** | OpenID Connect — identity standard used for attestation JWTs. |
| **SDP** | Session Definition Protocol — planned future extension for policy pre-agreement. |
| **creator_token** | A static secret token configured on the TEE server (`--creator_token`) that gates `CreateSession`. Only clients presenting the correct token can create sessions. |
| **invitation_token** | A cryptographically random 256-bit hex token returned by `CreateSession`. The creator shares this with other participants so they can call `JoinSession`. |
| **participant_token** | A cryptographically random 256-bit hex token issued to each participant upon `CreateSession` or `JoinSession`. Used to authenticate all subsequent session RPCs. Secret — must not be shared. |
