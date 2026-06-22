#!/bin/bash
# Helper to regenerate protos using a local virtualenv.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$HOME/.ztab-venv"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtualenv at $VENV_DIR..."
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install -r "$SCRIPT_DIR/requirements.txt"
fi

echo "Regenerating protos..."
"$VENV_DIR/bin/python3" "$SCRIPT_DIR/generate_protos.py"
