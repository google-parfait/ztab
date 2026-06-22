#!/bin/bash
# Sets up a Python virtual environment and outputs the MCP configuration block.
#
# Usage:
#   ./agent/install_mcp.sh [venv_path]
#   ./agent/install_mcp.sh --add-backend ID HOST PORT [--verifier TYPE] \
#       [--digest DIGEST] [--allow-debug-tee] [--set-default]
#
# Default venv_path is ~/.ztab-venv

set -e

# --- CLI Parsing ---
MODE="install"
VENV_PATH=""
BACKEND_ID=""
BACKEND_HOST=""
BACKEND_PORT=""
BACKEND_VERIFIER="noop"
BACKEND_DIGEST=""
BACKEND_ALLOW_DEBUG="true"
BACKEND_SET_DEFAULT="false"
REGISTER_MCP="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --add-backend)
      MODE="add-backend"
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
    --set-default)
      BACKEND_SET_DEFAULT="true"; shift
      ;;
    --register)
      REGISTER_MCP="true"; shift
      ;;
    --help|-h)
      echo "Usage:"
      echo "  Install mode:     ./install_mcp.sh [--register] [venv_path]"
      echo "  Add backend mode: ./install_mcp.sh --add-backend ID HOST PORT [options]"
      echo ""
      echo "Options for install mode:"
      echo "  --register            Auto-register ztab in ~/.gemini/config/mcp_config.json"
      echo ""
      echo "Options for --add-backend:"
      echo "  --verifier TYPE       Verifier type: noop|ita (default: noop)"
      echo "  --digest DIGEST       Expected image digest (default: empty)"
      echo "  --allow-debug-tee     Allow debug TEE (default)"
      echo "  --no-debug-tee        Reject debug TEE (for production)"
      echo "  --set-default         Make this the default backend"
      exit 0
      ;;
    *)
      # Positional arg: venv_path (install mode only)
      if [ "$MODE" = "install" ] && [ -z "$VENV_PATH" ]; then
        VENV_PATH="$1"
      fi
      shift
      ;;
  esac
done

# --- Add Backend Mode ---
if [ "$MODE" = "add-backend" ]; then
  if [ -z "$BACKEND_ID" ] || [ -z "$BACKEND_HOST" ] || [ -z "$BACKEND_PORT" ]; then
    echo "Error: --add-backend requires ID HOST PORT arguments." >&2
    echo "Usage: ./install_mcp.sh --add-backend my-tee 10.0.0.1 8000" >&2
    exit 1
  fi

  BACKENDS_DIR="$HOME/.ztab"
  BACKENDS_FILE="${BACKENDS_DIR}/backends.json"
  mkdir -p "$BACKENDS_DIR"

  NEW_ENTRY=$(cat <<ENTRY_EOF
{
  "backend_id": "${BACKEND_ID}",
  "name": "${BACKEND_ID}",
  "host": "${BACKEND_HOST}",
  "port": ${BACKEND_PORT},
  "verifier": "${BACKEND_VERIFIER}",
  "expected_digest": "${BACKEND_DIGEST}",
  "allow_debug_tee": ${BACKEND_ALLOW_DEBUG}
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
  exit 0
fi

# --- Install Mode ---
VENV_PATH="${VENV_PATH:-${VENV_PATH:-$HOME/.ztab-venv}}"
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
pip install --upgrade pip
pip install -r "${AGENT_DIR}/requirements.txt" || {
  echo "Falling back to direct PyPI index (e.g. if on a restricted Corp network)..."
  pip install --extra-index-url https://pypi.org/simple/ -r "${AGENT_DIR}/requirements.txt"
}

BACKENDS_DIR="$HOME/.ztab"
BACKENDS_FILE="${BACKENDS_DIR}/backends.json"

if [ ! -f "$BACKENDS_FILE" ]; then
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

# --- Register in MCP config (if --register) ---
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
  echo "Or re-run with --register to do this automatically."
fi
