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

# Build and run the ZTAB server locally via Docker.
# Uses Bazel-native OCI image (no Dockerfile).
#
# Usage:
#   ./run_server.sh                                              # echo-only mode
#   ./run_server.sh --llm --gcs_bucket gs://my-bucket            # Gemma 4 E2B
#   ./run_server.sh --model gemma4_e4b --gcs_bucket gs://my-bucket
#   ./run_server.sh --gpu                                        # GPU passthrough
#   ./run_server.sh --port 9000                                  # custom port
#   ./run_server.sh -d                                           # run in background

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

MODEL=""
PORT=8000
GPU_ARGS=""
DETACHED=""
GCS_BUCKET=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --llm)     MODEL="gemma4_e2b"; shift ;;
    --model)   MODEL="$2"; shift 2 ;;
    --model=*) MODEL="${1#*=}"; shift ;;
    --port)    PORT="$2"; shift 2 ;;
    --port=*)  PORT="${1#*=}"; shift ;;
    --gpu)         GPU_ARGS="--gpus all"; shift ;;
    --gcs_bucket)  GCS_BUCKET="$2"; shift 2 ;;
    --gcs_bucket=*) GCS_BUCKET="${1#*=}"; shift ;;
    -d)            DETACHED="-d"; shift ;;
    *)
      if [[ "$1" == "llm" ]]; then
        echo "WARNING: Positional argument 'llm' is deprecated. Please use '--llm'." >&2
        MODEL="gemma4_e2b"
        shift
      elif [[ "$1" == "gpu" ]]; then
        echo "WARNING: Positional argument 'gpu' is deprecated. Please use '--gpu'." >&2
        GPU_ARGS="--gpus all"
        shift
      elif [[ "$1" =~ ^[0-9]+$ ]]; then
        echo "WARNING: Positional argument for port is deprecated. Please use '--port $1'." >&2
        PORT="$1"
        shift
      elif [[ "$1" != -* ]]; then
        if [[ -z "${MODEL}" && "$1" != gs://* ]]; then
          echo "WARNING: Positional model argument is deprecated. Please use '--model $1'." >&2
          MODEL="$1"
          shift
        elif [[ -z "${GCS_BUCKET}" && "$1" == gs://* ]]; then
          echo "WARNING: Positional bucket argument is deprecated. Please use '--gcs_bucket $1'." >&2
          GCS_BUCKET="$1"
          shift
        else
          echo "Unknown positional argument: $1" >&2
          exit 1
        fi
      else
        echo "Unknown flag: $1" >&2
        exit 1
      fi
      ;;
  esac
done

if [[ -z "${MODEL}" ]]; then
    LOAD_TARGET=":ztab_server_echo_tarball"
    DOCKER_TAG="ztab-server-echo:latest"
    BAZEL_EXTRA_ARGS=""
else
    if [[ -z "${GCS_BUCKET}" ]]; then
      echo "ERROR: --gcs_bucket is required when using --llm or --model."
      echo "Example: ./run_server.sh --llm --gcs_bucket gs://your-model-bucket"
      exit 1
    fi
    LOAD_TARGET=":ztab_server_local_${MODEL}_tarball"
    DOCKER_TAG="ztab-server-local-${MODEL}:latest"
    BAZEL_EXTRA_ARGS="--repo_env=GCS_MODEL_BUCKET=${GCS_BUCKET}"
fi

CONTAINER_NAME="ztab-server"

echo "══════════════════════════════════════════════════════════════"
if [[ -z "${MODEL}" ]]; then
  echo "  Mode:   echo-only"
else
  echo "  Mode:   LLM (${MODEL})"
fi
echo "  Port:   ${PORT}"
echo "  Image:  ${DOCKER_TAG}"
echo "══════════════════════════════════════════════════════════════"

# Step 1: Build OCI image and load into Docker.
echo ""
echo "==> Building and loading OCI image..."
bazelisk run ${BAZEL_EXTRA_ARGS} "${LOAD_TARGET}"

# Stop existing container if running.
if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  echo "Stopping existing container ${CONTAINER_NAME}..."
  docker stop "${CONTAINER_NAME}" >/dev/null || true
  docker rm "${CONTAINER_NAME}" >/dev/null || true
fi

# Step 2: Run the container.
echo ""
echo "==> Starting server on port ${PORT}..."
if [[ "${DETACHED}" == "-d" ]]; then
  docker run --rm -d -p "${PORT}:8000" ${GPU_ARGS} \
    --name "${CONTAINER_NAME}" "${DOCKER_TAG}"
  echo "Server is running in background. Logs:"
  echo "  docker logs -f ${CONTAINER_NAME}"
else
  echo "Press Ctrl+C to stop the server."
  docker run --rm -p "${PORT}:8000" ${GPU_ARGS} \
    --name "${CONTAINER_NAME}" "${DOCKER_TAG}"
fi
