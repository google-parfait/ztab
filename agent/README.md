# ZTAB Agent Client

Python client library, CLI tools, MCP server, and agent skill
definition for connecting to a ZTAB TEE broker.

This code runs **locally on the user's machine** (or inside an
agent framework like
[Antigravity](https://github.com/google-gemini/antigravity) or
[Gemini CLI](https://github.com/google-gemini/gemini-cli)).
It is responsible for:

*   Establishing a secure gRPC channel to the ZTAB server.
*   Extracting and verifying the server's Remote Attestation
    token from its TLS certificate.
*   Exposing ZTAB capabilities to autonomous agents via the
    [Model Context Protocol (MCP)][mcp].

[mcp]: https://modelcontextprotocol.io/introduction

## How RA-TLS Verification Works (Client Side)

When connecting to a ZTAB server, the client performs the
following steps:

1.  Opens a raw TCP+TLS connection to the server (accepting
    self-signed certs).
2.  Retrieves the server's X.509 certificate from the TLS
    handshake.
3.  Extracts the custom extension at OID
    `1.3.6.1.4.1.99999.1`, which contains the attestation
    token (an OIDC JWT) wrapped in ASN.1 `OCTET STRING`
    encoding.
4.  Parses the ASN.1 tag+length headers to extract the raw
    JWT.
5.  Decodes the JWT payload and verifies the claims (issuer,
    hardware model, secure boot status, container image
    digest).
6.  Verifies that the `eat_nonce` claim matches the SHA-256
    hash of the server's TLS public key — proving the key was
    generated inside the attested TEE.
7.  If verification passes, establishes a gRPC channel using
    the server's certificate as the trusted root.

In production, the JWT signature will also be verified against
Google's Confidential Computing OIDC public keys.

## Source Files

| File | Description |
| :--- | :--- |
| `client.py` | Core library. `ZtabChannel` class: TLS cert fetching, ASN.1 attestation extraction, gRPC channel setup. Pluggable `verifier` callbacks. |
| `mcp_server.py` | Stdio-based MCP server exposing `ztab_test_connection`. Redirects `stdout` to `stderr` to protect JSON-RPC. |
| `SKILL.md` | Agent skill definition. Tells agents (e.g., Gemini CLI, Antigravity) how to bootstrap ZTAB. |
| `cli.py` | Standalone CLI tester. Connect, verify attestation, call `Echo`. |
| `diagnose_tls.py` | TLS diagnostic (no `grpcio` needed). Uses `ssl` + `cryptography` only. |
| `smoke_test.py` | Automated MCP smoke test. JSON-RPC lifecycle validation. |
| `generate_protos.py` | Compile `session_manager.proto` → `pb2/` stubs. |
| `pb2/` | Generated protobuf and gRPC stubs. |
| `requirements.txt` | Python deps: `grpcio`, `grpcio-tools`, `cryptography`. |

## Setup

### Prerequisites

*   Python 3.10+
*   A running ZTAB server (see `tee/README.md`)

### Install Dependencies

```bash
pip install -r requirements.txt
```

On restricted environments where system-level `pip install` is
not available, use a virtual environment:

```bash
python3 -m venv /tmp/ztab-venv
/tmp/ztab-venv/bin/pip install -r requirements.txt
```

### Compile Proto Stubs

```bash
python3 generate_protos.py
```

This generates `pb2/session_manager_pb2.py` and
`pb2/session_manager_pb2_grpc.py` from
`tee/session_manager.proto`.

## Usage

### CLI Tester

```bash
python3 cli.py --host localhost --port 8000 \
    --message "Hello ZTAB"
```

This connects to the server, extracts the attestation report,
calls `Echo`, and prints the response.

### TLS Diagnostics (no grpcio needed)

```bash
python3 diagnose_tls.py --host localhost --port 8000
```

This tests raw TLS connectivity and extracts the attestation
JWT without requiring `grpcio`. Useful for verifying the server
is presenting a valid certificate before debugging gRPC-level
issues.

### MCP Server

The MCP server runs over stdio and is intended to be launched
by an agent framework (e.g., Gemini CLI or Antigravity):

```bash
python3 mcp_server.py
```

To register it with an MCP client, add the server to your
client's MCP configuration. For example:

```json
{
  "mcpServers": {
    "ztab": {
      "command": "python3",
      "args": ["/absolute/path/to/ztab/agent/mcp_server.py"]
    }
  }
}
```

### Smoke Test

Run the automated MCP protocol validation (requires a running
ZTAB server):

```bash
python3 smoke_test.py --host localhost --port 8000
```

This validates the full MCP lifecycle: `initialize` →
`tools/list` → `tools/call` (`ztab_test_connection`).

## Architecture Notes

### Attestation Verification Pipeline

```
┌─────────────┐   TLS 1.3    ┌───────────────┐
│  Agent      │◄────────────►│  ZTAB Server  │
│  (client.py)│              │  (in TEE)     │
└──────┬──────┘              └───────────────┘
       │
       │ 1. Extract cert from TLS handshake
       │ 2. Find OID 1.3.6.1.4.1.99999.1
       │ 3. Strip ASN.1 OCTET STRING wrapper
       │ 4. Decode JWT payload
       │ 5. Verify eat_nonce == SHA-256(pubkey)
       │ 6. Verify JWT signature (prod only)
       │ 7. Check claims (iss, hwmodel, ...)
       ▼
  ┌──────────┐
  │ Verified │──► Establish gRPC channel
  └──────────┘
```

### Pluggable Verifiers

`ZtabChannel` accepts a `verifier` callback that receives the
decoded attestation claims dict and can accept or reject the
connection. This allows different trust policies:

*   `noop_verifier` — accepts all (development/testing).
*   Custom verifiers can check specific `image_digest` values,
    require `secboot: true`, or validate the JWT signature
    against known public keys.

### stdout Protection

`mcp_server.py` redirects Python's `stdout` to `stderr` at
import time. This prevents any `print()` calls in the codebase
from corrupting the JSON-RPC stream between the MCP server and
the agent framework. All MCP responses are written directly to
the original `stdout` file descriptor.
