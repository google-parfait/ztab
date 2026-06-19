#!/bin/bash
# Sets up a Python virtual environment and outputs the MCP configuration block.
#
# Usage:
#   ./agent/install_mcp.sh [venv_path]
#
# Default venv_path is ~/.ztab-venv

set -e

VENV_PATH="${1:-$HOME/.ztab-venv}"
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
