---
name: ztab
description: >-
  Verify secure TLS connectivity and remote attestation for
  the ZTAB Broker. Use when you need to confirm that a ZTAB
  TEE (Trusted Execution Environment) broker is reachable,
  presenting a valid remote attestation report (JWT), and
  accepting secure gRPC channel requests.
---

# ZTAB — Zero-Trust Agent Broker (Connectivity Skill)

This skill allows the agent to test secure TLS connectivity
to the ZTAB TEE broker, extract its remote attestation token,
and make secure gRPC calls.

## Available Tools

| Tool | Description |
| :--- | :--- |
| `ztab_test_connection` | Connect to a ZTAB server, extract the attestation token, decode it, and call Echo RPC to verify the channel. |

### Tool Details

#### `ztab_test_connection`

- `host` (string, required): Hostname or IP of the ZTAB
  server.
- `port` (integer, required): Port of the ZTAB server
  (default: `8000`).
- `message` (string, required): Test message to send via the
  Echo RPC.

Returns a JSON object containing the TLS connection status,
the extracted attestation JWT payload (mock claims), and the
Echo response.

## Bootstrapping (First-Time Setup)

To use the ZTAB tool, you must register the ZTAB MCP server
in your local agent configuration.

1. **Install dependencies**:
   ```bash
   pip install -r agent/requirements.txt
   ```

2. **Register the MCP Server**:
   Add the following to your MCP configuration file (e.g.,
   `mcp_config.json`):

   ```json
   {
     "mcpServers": {
       "ztab": {
         "command": "python3",
         "args": ["/path/to/ztab/agent/mcp_server.py"]
       }
     }
   }
   ```

   Replace `/path/to/ztab/` with the actual path to your
   cloned ZTAB repository.

3. **Restricted pip environments**:
   If `pip install` is restricted on your system, set up a
   virtual environment:
   ```bash
   python3 -m venv /tmp/ztab-venv
   /tmp/ztab-venv/bin/pip install -r agent/requirements.txt
   ```
   Then update the `command` in your MCP config to point to
   `/tmp/ztab-venv/bin/python3`.
