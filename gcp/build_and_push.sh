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

# Build the ZTAB server OCI image and push to Artifact Registry.
#
# All deployment-specific parameters (registry, GCS bucket) are required
# flags — this script has no hardcoded defaults. Deployment-specific
# wrappers should call this script with the appropriate flags.
#
# Usage:
#   gcp/build_and_push.sh \
#       --ztab_dir <path_to_ztab> \
#       --image_base us-docker.pkg.dev/myproject/myrepo/ztab-server \
#       --gcs_bucket gs://my-model-weights \
#       --model gemma4_e4b
#
#   gcp/build_and_push.sh \
#       --ztab_dir <path_to_ztab> \
#       --image_base us-docker.pkg.dev/myproject/myrepo/ztab-server \
#       --no_model

set -euo pipefail

ZTAB_DIR=""
MODEL="gemma4_e4b"
NO_MODEL=false
IMAGE_BASE=""
GCS_MODEL_BUCKET=""

usage() {
  cat <<EOF
Usage: $0 --ztab_dir <path> --image_base <url> [flags]

Build the ZTAB server OCI image and push to Artifact Registry.
Model weights are pulled from GCS via Bazel's gcs_file repo rule.

Required flags:
  --ztab_dir PATH       Path to the OSS ztab repo checkout.
  --image_base URL      Artifact Registry base (e.g., us-docker.pkg.dev/project/repo/image).

Optional flags:
  --model MODEL         Model to include (default: gemma4_e4b).
                        Available: gemma4_e2b, gemma4_e4b, gemma4_31b
  --no_model            Build without model (echo-only image).
  --gcs_bucket URL      GCS model bucket (required unless --no_model).
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ztab_dir)       ZTAB_DIR="$2"; shift 2 ;;
    --ztab_dir=*)     ZTAB_DIR="${1#*=}"; shift ;;
    --model)          MODEL="$2"; shift 2 ;;
    --model=*)        MODEL="${1#*=}"; shift ;;
    --no_model)       NO_MODEL=true; shift ;;
    --image_base)     IMAGE_BASE="$2"; shift 2 ;;
    --image_base=*)   IMAGE_BASE="${1#*=}"; shift ;;
    --gcs_bucket)     GCS_MODEL_BUCKET="$2"; shift 2 ;;
    --gcs_bucket=*)   GCS_MODEL_BUCKET="${1#*=}"; shift ;;
    --help|-h)        usage ;;
    *)
      echo "Unknown flag: $1"
      usage
      ;;
  esac
done

# Validate required flags.
if [[ -z "${ZTAB_DIR}" ]]; then
  echo "ERROR: --ztab_dir is required."
  usage
fi

if [[ -z "${IMAGE_BASE}" ]]; then
  echo "ERROR: --image_base is required."
  usage
fi

if [[ ! -f "${ZTAB_DIR}/MODULE.bazel" ]]; then
  echo "ERROR: ${ZTAB_DIR}/MODULE.bazel not found."
  echo "       MODULE.bazel should be at the repo root."
  exit 1
fi

if [[ "${NO_MODEL}" == "true" ]]; then
  LOAD_TARGET="//gcp:ztab_server_echo_tarball"
  LOCAL_TAG="ztab-server-echo:latest"
  IMAGE="${IMAGE_BASE}:echo_latest"

  echo "══════════════════════════════════════════════════════════════"
  echo "  Mode:         echo-only (no model)"
  echo "  Image:        ${IMAGE}"
  echo "══════════════════════════════════════════════════════════════"
else
  if [[ -z "${GCS_MODEL_BUCKET}" ]]; then
    echo "ERROR: --gcs_bucket is required when building with a model."
    usage
  fi

  LOAD_TARGET="//gcp:load_and_print_digest_runner_ztab_server_gcp_${MODEL}"
  LOCAL_TAG="ztab-server-gcp-${MODEL}:latest"
  IMAGE="${IMAGE_BASE}:${MODEL}_latest"

  echo "══════════════════════════════════════════════════════════════"
  echo "  Model:        ${MODEL}"
  echo "  GCS bucket:   ${GCS_MODEL_BUCKET}"
  echo "  Load target:  ${LOAD_TARGET}"
  echo "  Image:        ${IMAGE}"
  echo "══════════════════════════════════════════════════════════════"
fi

# ─── Step 1: Build OCI image and load into Docker ─────────────────────────
echo ""
echo "==> Step 1/2: Building and loading OCI image..."
echo "    (Model weights downloaded from ${GCS_MODEL_BUCKET} via Bazel)"

(cd "${ZTAB_DIR}" && bazelisk run -c opt \
  --//tee:enable_cuda=true \
  --repo_env=GCS_MODEL_BUCKET="${GCS_MODEL_BUCKET}" \
  "${LOAD_TARGET}")

# ─── Step 2: Tag and push to Artifact Registry ────────────────────────────
echo ""
echo "==> Step 2/2: Pushing image to Artifact Registry..."

docker tag "${LOCAL_TAG}" "${IMAGE}"
docker push "${IMAGE}"

DIGEST=$(docker inspect --format='{{index .RepoDigests 0}}' "${IMAGE}")

echo ""
echo "══════════════════════════════════════════════════════════════"
echo "  SUCCESS"
echo "══════════════════════════════════════════════════════════════"
echo "  Image:   ${IMAGE}"
echo "  Digest:  ${DIGEST}"
echo "══════════════════════════════════════════════════════════════"
