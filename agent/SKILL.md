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

1. **Install and Configure**:
   We provide a fully automated script that creates a secure virtual
   environment, installs all dependencies, and outputs the exact JSON 
   configuration block required for your system.

   Run the following command from the root of the ZTAB repository:
   ```bash
   bash agent/install_mcp.sh
   ```

2. **Register the MCP Server**:
   Copy the outputted JSON block from the script and paste it into
   your local agent's MCP configuration file (e.g., `~/.gemini/mcp_config.json`, though the exact path varies depending on your agent environment).
