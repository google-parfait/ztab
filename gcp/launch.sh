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

# Manage a Confidential Space VM with H100 + ITA for ZTAB.
#
# All deployment-specific parameters (project, zone, image registry) are
# required flags — this script has no hardcoded defaults.
#
# Modes: setup, launch, get-ip, delete
#
# Usage:
#   gcp/launch.sh \
#       --project myproject \
#       --zone us-east5-a \
#       --image_base us-docker.pkg.dev/myproject/myrepo/ztab-server \
#       --ita_api_key API_KEY \
#       --mode setup
#
#   gcp/launch.sh \
#       --project myproject \
#       --zone us-east5-a \
#       --mode launch

set -euo pipefail

MODE="launch"
ITA_API_KEY=""
PROJECT=""
ZONE=""
IMAGE_BASE=""
MODEL="gemma4_e4b"
IMAGE_TAG=""  # derived from MODEL below
INSTANCE_NAME="ztab-server-h100"
TEMPLATE_NAME="ztab-server-h100-template"
GROUP_NAME="ztab-server-h100-mig"
CREATOR_TOKEN=""
DEBUG="true"

usage() {
  cat <<EOF
Usage: $0 [flags]

Manage a Confidential Space VM with H100 + ITA for ZTAB.

Required flags:
  --project PROJECT     GCP project ID.
  --zone ZONE           GCP zone (e.g., us-east5-a).

Required for setup:
  --image_base URL      Artifact Registry base URL.
  --ita_api_key KEY     Intel Trust Authority API key.

Optional flags:
  --mode MODE           Mode: setup, launch, get-ip, delete (default: launch).
  --model MODEL         Model name (default: gemma4_e4b).
  --latest              Use the :MODEL_latest tag instead of :MODEL_staging.
  --image_tag TAG       Override image tag explicitly.
  --instance_name NAME  Override instance name (default: ztab-server-h100).
  --template_name NAME  Override template name (default: ztab-server-h100-template).
  --group_name NAME     Override MIG group name (default: ztab-server-h100-mig).
  --debug BOOL          Use debug Confidential Space image (default: true).
EOF
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)           MODE="$2"; shift 2 ;;
    --mode=*)         MODE="${1#*=}"; shift ;;
    --ita_api_key)    ITA_API_KEY="$2"; shift 2 ;;
    --ita_api_key=*)  ITA_API_KEY="${1#*=}"; shift ;;
    --project)        PROJECT="$2"; shift 2 ;;
    --project=*)      PROJECT="${1#*=}"; shift ;;
    --zone)           ZONE="$2"; shift 2 ;;
    --zone=*)         ZONE="${1#*=}"; shift ;;
    --image_base)     IMAGE_BASE="$2"; shift 2 ;;
    --image_base=*)   IMAGE_BASE="${1#*=}"; shift ;;
    --model)          MODEL="$2"; shift 2 ;;
    --model=*)        MODEL="${1#*=}"; shift ;;
    --latest)         IMAGE_TAG="latest"; shift ;;
    --image_tag)      IMAGE_TAG="$2"; shift 2 ;;
    --image_tag=*)    IMAGE_TAG="${1#*=}"; shift ;;
    --instance_name)  INSTANCE_NAME="$2"; shift 2 ;;
    --instance_name=*) INSTANCE_NAME="${1#*=}"; shift ;;
    --template_name)  TEMPLATE_NAME="$2"; shift 2 ;;
    --template_name=*) TEMPLATE_NAME="${1#*=}"; shift ;;
    --group_name)     GROUP_NAME="$2"; shift 2 ;;
    --group_name=*)   GROUP_NAME="${1#*=}"; shift ;;
    --debug)          DEBUG="true"; shift ;;
    --debug=*)        DEBUG="${1#*=}"; shift ;;
    --creator_token)  CREATOR_TOKEN="$2"; shift 2 ;;
    --creator_token=*) CREATOR_TOKEN="${1#*=}"; shift ;;
    --help|-h)        usage ;;
    *)
      echo "Unknown flag: $1"
      usage
      ;;
  esac
done

# Validate required flags.
if [[ -z "${PROJECT}" ]]; then
  echo "ERROR: --project is required."
  usage
fi

if [[ -z "${ZONE}" ]]; then
  echo "ERROR: --zone is required."
  usage
fi

# Derive image tag: explicit override > --latest > default (MODEL_staging).
if [[ -z "${IMAGE_TAG}" ]]; then
  IMAGE_TAG="${MODEL}_staging"
elif [[ "${IMAGE_TAG}" == "latest" ]]; then
  IMAGE_TAG="${MODEL}_latest"
fi

if [[ -n "${IMAGE_BASE}" ]]; then
  IMAGE="${IMAGE_BASE}:${IMAGE_TAG}"
fi

function log_and_run() {
  echo -e "\n\033[1;33m[CMD] Executing:\033[0m $*" >&2
  "$@"
}

case "${MODE}" in
  setup)
    if [[ -z "${ITA_API_KEY}" ]]; then
      echo "ERROR: --ita_api_key is required for setup mode."
      exit 1
    fi

    if [[ -z "${IMAGE_BASE}" ]]; then
      echo "ERROR: --image_base is required for setup mode."
      exit 1
    fi

    CS_IMAGE_NAME="confidential-space-260500"
    if [[ "${DEBUG}" == "true" ]]; then
      CS_IMAGE_NAME="confidential-space-debug-260500"
    fi
    OS_IMAGE="projects/confidential-space-images/global/images/${CS_IMAGE_NAME}"

    # Build metadata string.
    METADATA="tee-image-reference=${IMAGE}"
    if [[ "${DEBUG}" == "true" ]]; then
      METADATA="${METADATA},tee-container-log-redirect=true"
    fi
    METADATA="${METADATA},tee-container-ports=8000:8000"
    METADATA="${METADATA},tee-install-gpu-driver=true"
    METADATA="${METADATA},ita-api-key=${ITA_API_KEY}"
    METADATA="${METADATA},ita-region=US"
    if [[ -n "${CREATOR_TOKEN}" ]]; then
      METADATA="${METADATA},tee-env-CREATOR_TOKEN=${CREATOR_TOKEN}"
    fi

    echo "══════════════════════════════════════════════════════════════"
    echo "  Setting up Confidential Space VM Template"
    echo "══════════════════════════════════════════════════════════════"
    echo "  Template:   ${TEMPLATE_NAME}"
    echo "  MIG:        ${GROUP_NAME}"
    echo "  App Image:  ${IMAGE}"
    echo "  OS Image:   ${OS_IMAGE}"
    echo "  Project:    ${PROJECT}"
    echo "  Zone:       ${ZONE}"
    echo "══════════════════════════════════════════════════════════════"

    echo "Cleaning up old resources..."
    log_and_run gcloud compute instance-groups managed delete "${GROUP_NAME}" \
      --project="${PROJECT}" --zone="${ZONE}" --quiet || true

    log_and_run gcloud compute instance-templates delete "${TEMPLATE_NAME}" \
      --project="${PROJECT}" --quiet || true

    echo "Creating new Instance Template..."
    log_and_run gcloud beta compute instance-templates create "${TEMPLATE_NAME}" \
      --project="${PROJECT}" \
      --machine-type=a3-highgpu-1g \
      --accelerator=type=nvidia-h100-80gb,count=1 \
      --min-cpu-platform="Intel Sapphire Rapids" \
      --network-interface="nic-type=GVNIC,stack-type=IPV4_ONLY" \
      --confidential-compute-type=TDX \
      --shielded-secure-boot \
      --image="${OS_IMAGE}" \
      --boot-disk-size=200GB \
      --boot-disk-type=pd-ssd \
      --service-account="confidential-workload-sa@${PROJECT}.iam.gserviceaccount.com" \
      --scopes=cloud-platform \
      --tags=http-server,https-server \
      --maintenance-policy=TERMINATE \
      --instance-termination-action=DELETE \
      --max-run-duration=168h \
      --reservation-affinity=none \
      --provisioning-model=FLEX_START \
      --metadata="${METADATA}"

    echo "Creating Managed Instance Group..."
    log_and_run gcloud compute instance-groups managed create "${GROUP_NAME}" \
      --project="${PROJECT}" \
      --zone="${ZONE}" \
      --template="${TEMPLATE_NAME}" \
      --size=0 \
      --default-action-on-vm-failure=do_nothing

    echo "==> Setup complete. Run with --mode=launch to request a node."
    ;;

  launch)
    echo "══════════════════════════════════════════════════════════════"
    echo "  Launching Confidential Space VM (Requesting node)"
    echo "══════════════════════════════════════════════════════════════"
    echo "  MIG Group:  ${GROUP_NAME}"
    echo "  Project:    ${PROJECT}"
    echo "  Zone:       ${ZONE}"
    echo "══════════════════════════════════════════════════════════════"

    echo "==> Launching Confidential Space VM via DWS..."
    REQUEST_NAME="req-$(date +%s)"

    # Ensure group is size 0 first
    log_and_run gcloud compute instance-groups managed resize "${GROUP_NAME}" \
        --project="${PROJECT}" \
        --zone="${ZONE}" \
        --size=0 \
        --quiet

    log_and_run gcloud compute instance-groups managed resize-requests create "${GROUP_NAME}" \
        --project="${PROJECT}" \
        --zone="${ZONE}" \
        --resize-request="${REQUEST_NAME}" \
        --resize-by=1

    echo "==> Resize request submitted. Use --mode=get-ip to poll for readiness."
    ;;

  get-ip)
    echo "==> Polling for external IP (Timeout: 20 mins)..."
    TIMEOUT=1200
    START=${SECONDS}

    while true; do
      ELAPSED=$(( SECONDS - START ))
      if (( ELAPSED > TIMEOUT )); then
        echo "Timeout waiting for instance." >&2
        exit 1
      fi

      FOUND_NAME=$(gcloud compute instance-groups managed list-instances "${GROUP_NAME}" \
          --project="${PROJECT}" --zone="${ZONE}" --limit=1 --format="value(name)" 2>/dev/null || true)

      if [[ -n "${FOUND_NAME}" ]]; then
        STATUS=$(gcloud compute instances describe "${FOUND_NAME}" \
            --project="${PROJECT}" --zone="${ZONE}" --format="value(status)" 2>/dev/null || true)

        if [[ "${STATUS}" == "RUNNING" ]]; then
          IP=$(gcloud compute instances describe "${FOUND_NAME}" \
              --project="${PROJECT}" --zone="${ZONE}" --format='value(networkInterfaces[0].accessConfigs[0].natIP)')
          if [[ -n "${IP}" ]]; then
            echo -e "\n✅ Server RUNNING at ${IP}"
            exit 0
          fi
        fi
      fi
      sleep 10
    done
    ;;

  delete)
    echo "==> Deleting instance (resizing to 0)..."
    log_and_run gcloud compute instance-groups managed resize "${GROUP_NAME}" \
      --project="${PROJECT}" \
      --zone="${ZONE}" \
      --size=0

    echo "==> Instance spun down. Infrastructure remains."
    ;;

  *)
    echo "ERROR: Unknown mode '${MODE}'. Must be one of: setup, launch, get-ip, delete."
    exit 1
    ;;
esac
