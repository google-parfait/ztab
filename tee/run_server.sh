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

# Helper script to build and run the ZTAB server inside Docker.
#
# Usage:
#   ./run_server.sh [port] [-d]
#
# Options:
#   port   Port to bind on host (default: 8000)
#   -d     Run in background (detached mode)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

PORT="8000"
DETACHED=""

# Simple argument parsing.
for arg in "$@"; do
  if [ "$arg" = "-d" ]; then
    DETACHED="-d"
  else
    PORT="$arg"
  fi
done

CONTAINER_NAME="ztab-server"
IMAGE_NAME="ztab-server"

echo "Building Docker image ${IMAGE_NAME}..."
docker build -t "${IMAGE_NAME}" .

# Stop existing container if running.
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  echo "Stopping existing container ${CONTAINER_NAME}..."
  docker stop "${CONTAINER_NAME}" >/dev/null || true
  docker rm "${CONTAINER_NAME}" >/dev/null || true
fi

echo "Starting server on port ${PORT}..."
if [ "$DETACHED" = "-d" ]; then
  docker run --rm -d -p "${PORT}:${PORT}" --name "${CONTAINER_NAME}" "${IMAGE_NAME}" "${PORT}"
  echo "Server is running in background. Logs:"
  echo "  docker logs -f ${CONTAINER_NAME}"
else
  # Foreground mode: stream logs, stop on Ctrl+C.
  echo "Press Ctrl+C to stop the server."
  docker run --rm -p "${PORT}:${PORT}" --name "${CONTAINER_NAME}" "${IMAGE_NAME}" "${PORT}"
fi
