#!/bin/bash
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

# Installs the ZTAB MCP server: creates a Python virtual environment,
# installs dependencies, configures backends, and registers the MCP
# server in the agent's config.  One command does everything.
#
# Usage:
#   ./agent/install_mcp.sh [options]
#
# Examples:
#   # Basic install (default dev-local backend on localhost:8000):
#   bash agent/install_mcp.sh
#
#   # Install and configure a specific backend:
#   bash agent/install_mcp.sh --add-backend my-tee HOST PORT --set-default
#
#   # With admission control:
#   bash agent/install_mcp.sh --add-backend my-tee HOST PORT \
#       --creator-token TOKEN --set-default
#
# Default venv_path is ~/.ztab-venv

set -e

# --- CLI Parsing ---
VENV_PATH=""
BACKEND_ID=""
BACKEND_HOST=""
BACKEND_PORT=""
BACKEND_VERIFIER=""
BACKEND_DIGEST=""
BACKEND_ALLOW_DEBUG="true"
BACKEND_SET_DEFAULT="false"
BACKEND_CREATOR_TOKEN=""
REGISTER_MCP="true"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --add-backend)
      BACKEND_ID="$2"; BACKEND_HOST="$3"; BACKEND_PORT="$4"
      shift 4
      ;;
    --verifier)
      BACKEND_VERIFIER="$2"; shift 2
      ;;
    --digest)
      BACKEND_DIGEST="$2"; shift 2
      ;;
    --allow-debug-tee)
      BACKEND_ALLOW_DEBUG="true"; shift
      ;;
    --no-debug-tee)
      BACKEND_ALLOW_DEBUG="false"; shift
      ;;
    --creator-token)
      BACKEND_CREATOR_TOKEN="$2"; shift 2
      ;;
    --set-default)
      BACKEND_SET_DEFAULT="true"; shift
      ;;
    --do-not-register)
      REGISTER_MCP="false"; shift
      ;;
    --register)
      # Accepted for backward compatibility (already the default).
      REGISTER_MCP="true"; shift
      ;;
    --help|-h)
      echo "Usage:"
      echo "  ./install_mcp.sh [options]"
      echo ""
      echo "Installs the ZTAB MCP server (venv + deps + registration)."
      echo "If --add-backend is given, also configures that backend."
      echo ""
      echo "Options:"
      echo "  --add-backend ID HOST PORT  Configure a TEE backend"
      echo "  --verifier TYPE             Verifier: noop|ita (default: noop)"
      echo "  --digest DIGEST             Expected image digest (default: empty)"
      echo "  --allow-debug-tee           Allow debug TEE (default)"
      echo "  --no-debug-tee              Reject debug TEE (for production)"
      echo "  --creator-token TOKEN       Creator token for admission control"
      echo "  --set-default               Make this the default backend"
      echo "  --do-not-register           Skip mcp_config.json registration"
      exit 0
      ;;
    *)
      # Positional arg: venv_path
      if [ -z "$VENV_PATH" ]; then
        VENV_PATH="$1"
      fi
      shift
      ;;
  esac
done

# --- Step 1: Virtual Environment ---
VENV_PATH="${VENV_PATH:-$HOME/.ztab-venv}"
# Ensure absolute path using python (portable across Linux/macOS)
VENV_PATH=$(python3 -c "import os, sys; print(os.path.abspath(sys.argv[1]))" "$VENV_PATH")
# Ensure we know where the mcp_server.py is relative to this script
AGENT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MCP_SERVER_PATH="${AGENT_DIR}/mcp_server.py"

if [ -d "$VENV_PATH" ]; then
  echo "Existing virtual environment found at ${VENV_PATH}. Verifying..."
  if "${VENV_PATH}/bin/python3" -c "import cryptography, grpc, jwt" 2>/dev/null; then
    echo "Virtual environment is functional. Skipping creation."
  else
    echo "Virtual environment is corrupted or Python version changed. Recreating..."
    python3 -m venv --clear "${VENV_PATH}"
  fi
else
  echo "Creating virtual environment at ${VENV_PATH}..."
  python3 -m venv "${VENV_PATH}"
fi

echo "Activating virtual environment..."
source "${VENV_PATH}/bin/activate"

echo "Installing ZTAB dependencies..."
pip install --upgrade pip || true
pip install -r "${AGENT_DIR}/requirements.txt" || {
  echo "Falling back to direct PyPI index (e.g. if on a restricted Corp network)..."
  pip install --extra-index-url https://pypi.org/simple/ -r "${AGENT_DIR}/requirements.txt"
}

# --- Step 2: Backends Configuration ---
BACKENDS_DIR="$HOME/.ztab"
BACKENDS_FILE="${BACKENDS_DIR}/backends.json"

if [ -n "$BACKEND_ID" ]; then
  # --add-backend was specified: configure that backend.
  if [ -z "$BACKEND_HOST" ] || [ -z "$BACKEND_PORT" ]; then
    echo "Error: --add-backend requires ID HOST PORT arguments." >&2
    exit 1
  fi
  if [ -z "$BACKEND_VERIFIER" ]; then
    echo "Error: --verifier is required with --add-backend." >&2
    echo "  Use --verifier noop (local dev) or --verifier ita (production)." >&2
    exit 1
  fi

  mkdir -p "$BACKENDS_DIR"

  # Build optional creator_token line for JSON entry.
  CREATOR_TOKEN_LINE=""
  if [ -n "$BACKEND_CREATOR_TOKEN" ]; then
    CREATOR_TOKEN_LINE=",
  \"creator_token\": \"${BACKEND_CREATOR_TOKEN}\""
  fi

  NEW_ENTRY=$(cat <<ENTRY_EOF
{
  "backend_id": "${BACKEND_ID}",
  "name": "${BACKEND_ID}",
  "host": "${BACKEND_HOST}",
  "port": ${BACKEND_PORT},
  "verifier": "${BACKEND_VERIFIER}",
  "expected_digest": "${BACKEND_DIGEST}",
  "allow_debug_tee": ${BACKEND_ALLOW_DEBUG}${CREATOR_TOKEN_LINE}
}
ENTRY_EOF
)

  if [ ! -f "$BACKENDS_FILE" ]; then
    # Create new config with this as the only (and default) backend.
    # Write atomically: temp file + mv to prevent LS reading partial JSON.
    DEFAULT_ID="$BACKEND_ID"
    TMPFILE=$(mktemp "${BACKENDS_DIR}/.backends.json.XXXXXX")
    cat <<EOF > "$TMPFILE"
{
  "version": 1,
  "default_backend": "${DEFAULT_ID}",
  "backends": [
    ${NEW_ENTRY}
  ]
}
EOF
    mv "$TMPFILE" "$BACKENDS_FILE"
    echo "Created ${BACKENDS_FILE} with backend '${BACKEND_ID}'."
  else
    # Append to existing config using python (portable JSON manipulation).
    # Write atomically: temp file + os.replace() to prevent LS reading partial JSON.
    python3 -c "
import json, sys, os, tempfile

new_entry = json.loads('''${NEW_ENTRY}''')
with open('${BACKENDS_FILE}', 'r') as f:
    config = json.load(f)

# Remove existing entry with same backend_id if present.
config['backends'] = [b for b in config.get('backends', [])
                       if b.get('backend_id') != '${BACKEND_ID}']
config['backends'].append(new_entry)

if '${BACKEND_SET_DEFAULT}' == 'true':
    config['default_backend'] = '${BACKEND_ID}'

# Atomic write: temp file in same dir, then os.replace().
fd, tmp = tempfile.mkstemp(dir='${BACKENDS_DIR}', prefix='.backends.json.')
with os.fdopen(fd, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
os.replace(tmp, '${BACKENDS_FILE}')
"
    echo "Added/updated backend '${BACKEND_ID}' in ${BACKENDS_FILE}."
  fi

  if [ "$BACKEND_SET_DEFAULT" = "true" ]; then
    echo "Set '${BACKEND_ID}' as default backend."
  fi

elif [ ! -f "$BACKENDS_FILE" ]; then
  # No --add-backend and no existing config: create default.
  echo "Creating default ZTAB backends configuration at ${BACKENDS_FILE}..."
  mkdir -p "$BACKENDS_DIR"
  cat <<'EOF' > "$BACKENDS_FILE"
{
  "version": 1,
  "default_backend": "dev-local",
  "backends": [
    {
      "backend_id": "dev-local",
      "name": "Local Development Server",
      "description": "Local mock server for development. No attestation.",
      "host": "localhost",
      "port": 8000,
      "verifier": "noop",
      "expected_digest": "",
      "allow_debug_tee": true
    }
  ]
}
EOF
fi

# --- Step 3: Register in MCP config ---
if [ "$REGISTER_MCP" = "true" ]; then
  MCP_CONFIG="$HOME/.gemini/config/mcp_config.json"
  mkdir -p "$(dirname "$MCP_CONFIG")"

  python3 -c "
import json, os, tempfile

config_path = '$MCP_CONFIG'
try:
    with open(config_path) as f:
        config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    config = {}

config.setdefault('mcpServers', {})
config['mcpServers']['ztab'] = {
    'command': '${VENV_PATH}/bin/python3',
    'args': ['${MCP_SERVER_PATH}']
}

# Atomic write: temp file in same dir, then os.replace().
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(config_path),
                           prefix='.mcp_config.')
with os.fdopen(fd, 'w') as f:
    json.dump(config, f, indent=2)
    f.write('\n')
os.replace(tmp, config_path)
"
  echo "✅ Registered ztab in $MCP_CONFIG"
else
  echo ""
  echo "------------------------------------------------------------"
  echo "✅ ZTAB MCP Server Installation Successful!"
  echo "------------------------------------------------------------"
  echo "Add the following JSON block to your MCP configuration file"
  echo "to register the ZTAB broker in your agent's context:"
  echo ""
  cat <<EOF
    "ztab": {
      "command": "${VENV_PATH}/bin/python3",
      "args": ["${MCP_SERVER_PATH}"]
    }
EOF
  echo ""
  echo "------------------------------------------------------------"
  echo "Or re-run without --do-not-register to do this automatically."
fi
