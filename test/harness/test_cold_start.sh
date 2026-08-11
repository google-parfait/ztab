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

# ==============================================================================
# ZTAB Unified Cold-Start Harness
# ==============================================================================
# Usage:
#   1-agent bootstrap test (local native, fast):
#     ./test_cold_start.sh --ztab_dir <path_to_ztab> --num_agents 1
#
#   2-agent session test (local native, fast):
#     ./test_cold_start.sh --ztab_dir <path_to_ztab> --num_agents 2
#
#   Docker LLM mode (production-fidelity):
#     ./test_cold_start.sh --ztab_dir <path_to_ztab> --tee_mode docker_build
#
#   Connect to GCP Confidential Space VM with real attestation:
#     ./test_cold_start.sh --ztab_dir <path_to_ztab> --tee_mode connect \
#       --host <IP> --port 8000 --verifier ita
#
#   Auto-discover GCP VM via gcloud:
#     ./test_cold_start.sh --ztab_dir <path_to_ztab> --tee_mode gcp_discover \
#       --gcp_project X --gcp_zone Y --verifier ita
#
# TEE Modes (--tee_mode):
#   local_build   - Build and run ztab_server binary directly (default)
#   docker_build  - Build OCI image via run_server.sh
#   gcp_discover  - Use gcloud to find a running Confidential Space VM
#   connect       - Connect to an already-running TEE at --host:--port
#
# Verifier (--verifier):
#   noop  - No attestation (default, for mock/dev TEEs)
#   ita   - Intel Trust Authority attestation (production)
# ==============================================================================

set -euo pipefail

# --- Deployment Defaults ---
GCS_MODEL_BUCKET=""
MODEL_PATH=""
TEE_LOCAL_PORT="9003"
GCP_PROJECT=""
GCP_ZONE=""
IMAGE_BASE=""

# --- Configuration ---
ZTAB_DIR=""
GCS_BUCKET=""         # Override for Docker LLM mode; defaults to GCS_MODEL_BUCKET
TEE_HOST="127.0.0.1"
TEE_PORT="8000"
TEE_MODE=""           # local_build, docker_build, gcp_discover, or connect
VERIFIER=""           # noop or ita; auto-set if not specified
MODEL_NAME="MODEL_GOOGLE_GEMINI_2_5_FLASH_LITE"
EXPECTED_DIGEST=""

# --- Supported LS flags (passed via --ls_extra_flags) ---
#   --standalone, --csrf_token, --workspace_id, --app_data_dir
#   --cloud_code_endpoint, --persistent_mode, --http_server_port

LS_BIN=""             # Path to the LS binary (optional; defaults to /usr/local/bin/language_server)
WORKSPACE_ID=""       # Workspace ID folder URI (defaults to ZTAB_DIR in oss mode)
LS_EXTRA_FLAGS=""     # Extra flags for internal/wrapper usage (Mode A). Empty for OSS mode.
APP_DATA_DIR="antigravity-ide"  # Override via --app_data_dir for internal wrapper usage

STATIC_TOKEN="standalone-test-token"
NUM_AGENTS=1          # default: 1-agent bootstrap test
REUSE_DIR=""          # --reuse-dir <path> to reuse an existing run directory
AUDIT_NON_BLOCKING=0  # --audit-non-blocking: audit failures become warnings
AUDIT_FAILED=0        # set by run_trajectory_audit if audit fails
PHASE1_ONLY=0         # --phase1-only: only run install phase, skip session lifecycle
DIRTY_BATTERY=0       # --dirty-state-battery: run D-series dirty state tests after normal run
CREATOR_TOKEN=""      # --creator_token: pre-shared token for gated CreateSession
AGENT_IDS=""          # Phase 1 cascade IDs (set by trigger_agents)
PHASE2_IDS=""         # Phase 2 cascade IDs (set by trigger_agents, may be empty)

declare -a AGENT_PORTS
declare -a AGENT_LS_PIDS

TEE_BUILD_PID=""
TEE_NATIVE_PID=""
STARTED_TEE_NATIVE=0

# Set up Run Directory
RUN_TIMESTAMP=$(TZ="America/Los_Angeles" date '+%Y-%m-%d_%H-%M')
RUN_DIR=""

# --- Helpers ---
log() {
  if [[ -n "$RUN_DIR" ]]; then
    echo "$(TZ="America/Los_Angeles" date '+%H:%M:%S') $*" | tee -a "$RUN_DIR/harness.log"
  else
    echo "$(TZ="America/Los_Angeles" date '+%H:%M:%S') $*"
  fi
}

phase() {
  local name="$1"; shift
  log "▶ Phase: $name"
  if ! "$@"; then
    log "✘ FAILED: $name"
    log "  See logs in: $RUN_DIR"
    exit 1
  fi
  log "✔ $name"
}

cleanup() {
  log "Cleaning up..."

  # Kill all agent LS instances
  for pid in "${AGENT_LS_PIDS[@]}"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
    [[ -n "$pid" ]] && wait "$pid" 2>/dev/null || true
  done

  # Kill native TEE server if we started one
  if [[ "$STARTED_TEE_NATIVE" -eq 1 && -n "$TEE_NATIVE_PID" ]]; then
    kill "$TEE_NATIVE_PID" 2>/dev/null || true
  fi

  # Capture docker logs BEFORE removing container (fixes R7 cleanup race)
  if [[ "$TEE_MODE" != "gcp_discover" && "$TEE_MODE" != "connect" && -n "$RUN_DIR" ]]; then
    docker logs ztab-server >> "$RUN_DIR/tee_server.log" 2>&1 || true
  fi

  # Kill TEE container (only for local modes)
  if [[ "$TEE_MODE" != "gcp_discover" && "$TEE_MODE" != "connect" ]]; then
    docker rm -f ztab-server 2>/dev/null || true
  fi

  # No config restore needed — user's $HOME is never modified.

  # Always copy the TEE server log to run dir (even on failure) for local_build mode
  if [[ -n "$RUN_DIR" && "$TEE_MODE" == "local_build" ]]; then
    if [[ -f "/tmp/ztab_tee_session_server.log" ]]; then
      cp /tmp/ztab_tee_session_server.log "$RUN_DIR/tee_server.log" 2>/dev/null || true
    fi
    log "Done. Logs: $RUN_DIR"
  fi
}
trap cleanup EXIT

# --- Phase 1: Setup ---
setup_env() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ztab_dir) ZTAB_DIR="$2"; shift 2 ;;
      --ztab_dir=*) ZTAB_DIR="${1#*=}"; shift ;;
      --mode) echo "WARNING: --mode is deprecated and ignored. Use --num_agents instead." >&2; shift 2 ;;
      --mode=*) echo "WARNING: --mode is deprecated and ignored. Use --num_agents instead." >&2; shift ;;
      --tee_mode) TEE_MODE="$2"; shift 2 ;;
      --tee_mode=*) TEE_MODE="${1#*=}"; shift ;;
      --tee) echo "WARNING: --tee is deprecated. Use --tee_mode instead." >&2; TEE_MODE="$2"; shift 2 ;;
      --tee=*) echo "WARNING: --tee is deprecated. Use --tee_mode instead." >&2; TEE_MODE="${1#*=}"; shift ;;
      --verifier) VERIFIER="$2"; shift 2 ;;
      --verifier=*) VERIFIER="${1#*=}"; shift ;;
      --gcs_bucket) GCS_BUCKET="$2"; shift 2 ;;
      --gcs_bucket=*) GCS_BUCKET="${1#*=}"; shift ;;
      --model_path) MODEL_PATH="$2"; shift 2 ;;
      --model_path=*) MODEL_PATH="${1#*=}"; shift ;;
      --gcp_project) GCP_PROJECT="$2"; shift 2 ;;
      --gcp_project=*) GCP_PROJECT="${1#*=}"; shift ;;
      --gcp_zone) GCP_ZONE="$2"; shift 2 ;;
      --gcp_zone=*) GCP_ZONE="${1#*=}"; shift ;;
      --gcp_image_base) IMAGE_BASE="$2"; shift 2 ;;
      --gcp_image_base=*) IMAGE_BASE="${1#*=}"; shift ;;
      --host) TEE_HOST="$2"; shift 2 ;;
      --host=*) TEE_HOST="${1#*=}"; shift ;;
      --port) TEE_PORT="$2"; shift 2 ;;
      --port=*) TEE_PORT="${1#*=}"; shift ;;
      --model) MODEL_NAME="$2"; shift 2 ;;
      --model=*) MODEL_NAME="${1#*=}"; shift ;;
      --expected_digest) EXPECTED_DIGEST="$2"; shift 2 ;;
      --expected_digest=*) EXPECTED_DIGEST="${1#*=}"; shift ;;
      --num_agents) NUM_AGENTS="$2"; shift 2 ;;
      --num_agents=*) NUM_AGENTS="${1#*=}"; shift ;;
      --reuse-dir) REUSE_DIR="$2"; shift 2 ;;
      --reuse-dir=*) REUSE_DIR="${1#*=}"; shift ;;
      --audit-non-blocking) AUDIT_NON_BLOCKING=1; shift ;;
      --phase1-only) PHASE1_ONLY=1; shift ;;
      --dirty-state-battery) DIRTY_BATTERY=1; shift ;;
      --creator_token) CREATOR_TOKEN="$2"; shift 2 ;;
      --creator_token=*) CREATOR_TOKEN="${1#*=}"; shift ;;
      --app_data_dir) APP_DATA_DIR="$2"; shift 2 ;;
      --app_data_dir=*) APP_DATA_DIR="${1#*=}"; shift ;;
      --ls_extra_flags) LS_EXTRA_FLAGS="$2"; shift 2 ;;
      --ls_extra_flags=*) LS_EXTRA_FLAGS="${1#*=}"; shift ;;
      --ls_bin) LS_BIN="$2"; shift 2 ;;
      --ls_bin=*) LS_BIN="${1#*=}"; shift ;;
      --workspace_id) WORKSPACE_ID="$2"; shift 2 ;;
      --workspace_id=*) WORKSPACE_ID="${1#*=}"; shift ;;
      --help|-h)
        cat <<EOF
Usage: $0 --ztab_dir <path> [--ls_bin <path>] [flags]

TEE Mode (--tee_mode, where does the TEE server come from?):
  local_build   Build and run native binary directly (default)
  docker_build  Build OCI image via run_server.sh
  gcp_discover  Use gcloud to find a running GCP Confidential Space VM
  connect       Connect to an already-running TEE at --host:--port

Verifier (--verifier, what attestation to expect?):
  noop          No attestation, for mock/dev TEEs (default)
  ita           Intel Trust Authority attestation, for production TEEs

Flags:
  --ztab_dir PATH       (required) Path to the OSS ztab repo
  --ls_bin PATH         Path to LS binary (default: /usr/local/bin/language_server)
  --ls_extra_flags STR  Custom command-line flags to pass to Language Server
  --app_data_dir NAME   LS app data directory name (default: antigravity-ide)
  --workspace_id URI    Workspace ID folder URI (default: file://ZTAB_DIR)
  --num_agents N        Number of agents (default: 1)
  --tee_mode MODE       TEE mode (see above)
  --verifier TYPE       Verifier type (see above)
  --gcs_bucket URL      GCS model bucket (for docker_build mode)
  --model_path PATH     Local GGUF model path (for local_build mode)
  --gcp_project PROJECT GCP project ID (for gcp_discover mode)
  --gcp_zone ZONE       GCP zone (for gcp_discover mode)
  --gcp_image_base URL  GCP Artifact Registry base URL (for gcp_discover mode)
  --host HOST           TEE host (default: 127.0.0.1; for connect mode)
  --port PORT           TEE port (default: 8000; for connect mode)
  --model MODEL         LS model enum (default: MODEL_GOOGLE_GEMINI_2_5_FLASH_LITE)
  --expected_digest D   Expected container digest for attestation
  --reuse-dir PATH      Reuse existing run dir (dirty state)
  --audit-non-blocking  Audit failures become warnings (don't fail the test)
  --phase1-only         Only run Phase 1 (install), skip session lifecycle
  --dirty-state-battery Run D-series dirty state tests after normal run
  --creator_token TOKEN Pre-shared token for gated CreateSession (admission control)
EOF
        exit 0
        ;;
      -*) echo "ERROR: Unknown option $1" >&2; exit 1 ;;
      *) echo "ERROR: Unexpected argument $1" >&2; exit 1 ;;
    esac
  done

  # --- Validate required args ---
  if [[ -z "$ZTAB_DIR" ]]; then
    ZTAB_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
  fi
  export ZTAB_DIR

  # B.4: Default LS_BIN to /usr/local/bin/language_server (populated by Dockerfile)
  if [[ -z "$LS_BIN" ]]; then
    LS_BIN="/usr/local/bin/language_server"
    log "  Using default LS binary: $LS_BIN"
  fi

  # Apply workspace_id defaults and resolve WORKSPACE_DIR
  if [[ -n "$WORKSPACE_ID" ]]; then
    if [[ "$WORKSPACE_ID" =~ ^file://(.*) ]]; then
      WORKSPACE_DIR="${BASH_REMATCH[1]}"
    else
      WORKSPACE_DIR="$WORKSPACE_ID"
      WORKSPACE_ID="file://$WORKSPACE_DIR"
    fi
  else
    WORKSPACE_DIR="$(cd "$ZTAB_DIR" 2>/dev/null && pwd || echo "$ZTAB_DIR")"
    WORKSPACE_ID="file://$WORKSPACE_DIR"
  fi

  if [[ "$NUM_AGENTS" -lt 1 ]]; then
    echo "ERROR: --num_agents must be >= 1." >&2
    exit 1
  fi

  # --- Apply TEE mode defaults ---
  # Backward compatibility: map old names to new names
  case "$TEE_MODE" in
    native) TEE_MODE="local_build" ;;
    docker) TEE_MODE="docker_build" ;;
    gcp)    TEE_MODE="gcp_discover" ;;
    external) TEE_MODE="connect" ;;
  esac

  if [[ -z "$TEE_MODE" ]]; then
    TEE_MODE="local_build"
  fi

  if [[ "$TEE_MODE" != "local_build" && "$TEE_MODE" != "docker_build" && "$TEE_MODE" != "gcp_discover" && "$TEE_MODE" != "connect" ]]; then
    echo "ERROR: --tee_mode must be 'local_build', 'docker_build', 'gcp_discover', or 'connect'." >&2
    exit 1
  fi

  # --- Apply verifier defaults ---
  if [[ -z "$VERIFIER" ]]; then
    VERIFIER="noop"
  fi

  if [[ "$VERIFIER" != "noop" && "$VERIFIER" != "ita" ]]; then
    echo "ERROR: --verifier must be 'noop' or 'ita'." >&2
    exit 1
  fi


  # --- Apply GCS bucket default ---
  if [[ -z "$GCS_BUCKET" ]]; then
    GCS_BUCKET="$GCS_MODEL_BUCKET"
  fi

  # --- Validate Docker+LLM mode has a bucket ---
  if [[ "$TEE_MODE" == "docker_build" && -z "$GCS_BUCKET" ]]; then
    echo "ERROR: --gcs_bucket is required for --tee_mode docker_build." >&2
    exit 1
  fi

  # --- Validate local_build mode has a model file ---
  if [[ "$TEE_MODE" == "local_build" ]]; then
    if [[ ! -f "$MODEL_PATH" ]]; then
      echo "ERROR: Model not found at $MODEL_PATH" >&2
      echo "Download it first or specify --model_path." >&2
      exit 1
    fi
  fi

  ZTAB_DIR="$(cd "$ZTAB_DIR" 2>/dev/null && pwd || echo "$ZTAB_DIR")"

  # NUM_AGENTS already defaults to 1 at the top of the script.

  # --- Port allocation ---
  # B.3: Ports are now discovered at runtime via the LS discovery file.
  # We no longer pre-allocate ports; each LS binds to a random port
  # and writes it to a discovery JSON file. The port is read by
  # get_discovered_http_port() in harness_lib.py after the LS starts.
  # AGENT_PORTS is populated in start_all_ls() after discovery.

  # --- Set up run directory ---
  if [[ -n "$REUSE_DIR" ]]; then
    RUN_DIR="$REUSE_DIR"
    log "REUSE MODE: Using existing directory $RUN_DIR. State is NOT clean."
  else
    RUN_DIR="/tmp/ztab_runs/${RUN_TIMESTAMP}_${NUM_AGENTS}agent"
    mkdir -p "$RUN_DIR"

    # Create per-agent sandboxes
    for i in $(seq 1 $NUM_AGENTS); do
      local agent_home="$RUN_DIR/agents/$i/home"
      mkdir -p "$agent_home/.gemini/config"
      # mcp_config.json starts EMPTY — agent must populate it:
      echo '{}' > "$agent_home/.gemini/config/mcp_config.json"
      # Copy OAuth token if it exists to allow keyless standalone mode.
      # Use globbing to avoid internal naming references in OSS code.
      local token_src=""
      local token_count=0
      for f in "$HOME"/.gemini/*standalone-oauth-token; do
        if [[ -f "$f" ]]; then
          ((token_count++))
          if [[ -z "$token_src" ]] || [[ "$f" -nt "$token_src" ]]; then
            token_src="$f"
          fi
        fi
      done
      if [[ "$token_count" -gt 1 && "$i" -eq 1 ]]; then
        log "WARNING: Multiple tokens found in ~/.gemini/. Selecting newest: $(basename "$token_src")"
      fi

      if [[ -n "$token_src" ]]; then
        if [[ "$i" -eq 1 ]]; then
          local expiry_warn
          expiry_warn=$(python3 -c '
import sys, json, datetime
try:
    data = json.load(open(sys.argv[1]))
    token = data.get("token", {})
    exp = token.get("expiry", "")[:19]
    if exp and not token.get("refresh_token"):
        expiry = datetime.datetime.strptime(exp, "%Y-%m-%dT%H:%M:%S")
        if (expiry - datetime.datetime.utcnow()).total_seconds() < 1800:
            print("WARNING: standalone token expires soon or is expired. Run agy auth login.")
except Exception:
    pass
' "$token_src" 2>/dev/null || true)
          if [[ -n "$expiry_warn" ]]; then
            log "$expiry_warn"
          fi
        fi
        cp "$token_src" "$agent_home/.gemini/"
        log "  Copied OAuth token to agent $i sandbox"
      fi
      # .ztab/ is NOT created — agent must create it via install_mcp.sh.
      log "  Created sandbox for agent $i: $agent_home"
    done
  fi

  # Clean up stale server logs from previous runs
  rm -f "/tmp/ztab_tee_session_server.log"

  # Save run metadata for post-mortem analysis
  cat > "$RUN_DIR/metadata.json" <<EOF
{"tee_mode": "$TEE_MODE", "model_name": "$MODEL_NAME",
 "ztab_dir": "$ZTAB_DIR", "start_time": "$(TZ="America/Los_Angeles" date -Iseconds)",
 "tee_host": "$TEE_HOST", "tee_port": $TEE_PORT, "num_agents": $NUM_AGENTS,
 "ports": "discovered_at_runtime"}
EOF

  # Workspace already resolved in setup_env. Define SCRIPT_DIR for finding scripts.
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  # --- Pre-flight cleanup ---
  rm -f /tmp/ztab_tee_session_server.log /tmp/standalone_ls_session.log
  # Purge stale global coordination files from previous runs
  rm -f /tmp/ztab_session_coord.json /tmp/agent_state.json

  # --- Print configuration ---
  echo "=============================================="
  echo "ZTAB Unified Cold-Start Harness (v3)"
  echo "=============================================="
  echo "  TEE mode:      $TEE_MODE"
  echo "  Agents:        $NUM_AGENTS"
  echo "  Ports:         (discovered at runtime)"
  if [[ "$TEE_MODE" == "local_build" ]]; then
    echo "  Model:         $MODEL_PATH"
  elif [[ "$TEE_MODE" == "docker_build" ]]; then
    echo "  GCS bucket:    $GCS_BUCKET"
  fi
  echo "  TEE server:    $TEE_HOST:$TEE_PORT"
  echo "  ZTAB dir:      $ZTAB_DIR"
  echo "  Workspace:     $WORKSPACE_DIR"
  echo "  Agent model:   $MODEL_NAME"
  echo "  Run dir:       $RUN_DIR"
  echo ""
}

# --- Phase 2: TEE server ---

# Start TEE via Docker (OCI container via run_server.sh)
start_tee_docker() {
  local port="$1"
  shift
  local extra_args=("$@")
  local container_name="ztab-server"

  # Determine timeout based on mode
  local timeout_secs=120
  for arg in "${extra_args[@]}"; do
    if [[ "$arg" == "--llm" ]]; then
      timeout_secs=600  # LLM mode: OCI build + GCS download + model load
      break
    fi
  done

  # Start via run_server.sh
  (cd "$ZTAB_DIR" && bash tee/run_server.sh --port "$port" \
    "${extra_args[@]}" \
    > "$RUN_DIR/tee_server.log" 2>&1) &
  TEE_BUILD_PID=$!

  log "Waiting for TEE Docker server (timeout: ${timeout_secs}s)..."
  for i in $(seq 1 "$timeout_secs"); do
    # Check if container is running AND server reports ready
    if docker ps --format '{{.Names}}' | grep -q "^${container_name}$"; then
      if docker logs "$container_name" 2>&1 | grep -qE '(listening|ready|Started server)'; then
        log "TEE Docker server ready (${i}s)"
        return 0
      fi
    fi

    # Check if the build/launch process died
    if ! kill -0 "$TEE_BUILD_PID" 2>/dev/null; then
      if docker ps --format '{{.Names}}' | grep -q "^${container_name}$"; then
        if docker logs "$container_name" 2>&1 | grep -qE '(listening|ready|Started server)'; then
          log "TEE Docker server ready (${i}s)"
          return 0
        fi
      else
        log "ERROR: TEE build/launch process died and no container running."
        cat "$RUN_DIR/tee_server.log" >&2
        return 1
      fi
    fi

    if [[ $i -eq $timeout_secs ]]; then
      log "ERROR: TEE Docker server not ready after ${timeout_secs}s."
      docker logs "$container_name" >> "$RUN_DIR/tee_server.log" 2>&1 || true
      cat "$RUN_DIR/tee_server.log" >&2
      return 1
    fi
    sleep 1
  done
}

# Start TEE via native binary (bazel build + direct execution)
# Ported from test_session_cold_start.sh lines 167-202
start_tee_native() {
  local port="$1"

  log "Building TEE server (native)..."
  TEE_BIN="$ZTAB_DIR/bazel-bin/tee/ztab_server"
  
  (cd "$ZTAB_DIR" && bazelisk build -c opt //tee:ztab_server > "$RUN_DIR/tee_build.log" 2>&1) || {
    log "ERROR: TEE server build failed."
    cat "$RUN_DIR/tee_build.log" >&2
    return 1
  }

  if [[ ! -x "$TEE_BIN" ]]; then
    log "ERROR: TEE binary not found at $TEE_BIN after build."
    return 1
  fi
  log "  Binary: $TEE_BIN"

  log "Starting TEE server (native, model=$MODEL_PATH)..."
  local creator_flag=""
  if [[ -n "$CREATOR_TOKEN" ]]; then
    creator_flag="--creator_token=$CREATOR_TOKEN"
    log "  Admission control: GATED (creator_token set)"
  else
    log "  Admission control: UNGATED (no creator_token)"
  fi
  nohup "$TEE_BIN" \
    --attestation_provider=mock \
    --port="$port" \
    --local_port="$TEE_LOCAL_PORT" \
    --model_path="$MODEL_PATH" \
    --gpu_layers=0 \
    --policy_dir="$ZTAB_DIR/examples/calendar/" \
    $creator_flag \
    > /tmp/ztab_tee_session_server.log 2>&1 &
  TEE_NATIVE_PID=$!
  STARTED_TEE_NATIVE=1

  # Poll for readiness (15s — native starts fast)
  for i in $(seq 1 30); do
    if grep -q "listening" /tmp/ztab_tee_session_server.log 2>/dev/null; then
      log "TEE native server ready (PID $TEE_NATIVE_PID, ${i}s)"
      # Copy log to run dir for consistency
      cp /tmp/ztab_tee_session_server.log "$RUN_DIR/tee_server.log" 2>/dev/null || true
      return 0
    fi
    if ! kill -0 "$TEE_NATIVE_PID" 2>/dev/null; then
      log "ERROR: TEE native server process died."
      tail -20 /tmp/ztab_tee_session_server.log >&2 2>/dev/null || true
      return 1
    fi
    if [[ $i -eq 30 ]]; then
      log "ERROR: TEE native server failed to start in 30s."
      tail -20 /tmp/ztab_tee_session_server.log >&2 2>/dev/null || true
      return 1
    fi
    sleep 1
  done
}

# Start TEE via GCP — resolve VM IP via gcloud, verify reachability
start_tee_gcp_discover() {
  log "gcp_discover mode: looking up Confidential Space VM IP via gcloud..."
  TEE_PORT="8000"  # GCP Confidential Space always listens on 8000

  if [[ -z "$GCP_PROJECT" || -z "$GCP_ZONE" ]]; then
    log "ERROR: --gcp_project and --gcp_zone are required for --tee_mode gcp_discover."
    return 1
  fi

  local launch_args=("--project" "$GCP_PROJECT" "--zone" "$GCP_ZONE")
  if [[ -n "$IMAGE_BASE" ]]; then
    launch_args+=("--image_base" "$IMAGE_BASE")
  fi

  local gcp_ip
  gcp_ip=$(
    "$ZTAB_DIR/gcp/launch.sh" "${launch_args[@]}" --mode=get-ip 2>/dev/null \
    | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' \
    | head -1
  )

  if [[ -z "$gcp_ip" ]]; then
    local p_hint="${GCP_PROJECT:-YOUR_PROJECT}"
    local z_hint="${GCP_ZONE:-YOUR_ZONE}"
    local ib_hint="${IMAGE_BASE:-YOUR_IMAGE_BASE}"
    log "ERROR: No running GCP Confidential Space VM found."
    log "Launch one first:"
    log "  $ZTAB_DIR/gcp/launch.sh --project $p_hint --zone $z_hint --image_base $ib_hint --mode setup --ita_api_key=YOUR_KEY"
    log "  $ZTAB_DIR/gcp/launch.sh --project $p_hint --zone $z_hint --mode launch"
    return 1
  fi

  TEE_HOST="$gcp_ip"
  log "  Found GCP VM at $TEE_HOST:$TEE_PORT"

  # Verify reachability
  if ! timeout 5 bash -c "echo > /dev/tcp/$TEE_HOST/$TEE_PORT" >/dev/null 2>&1; then
    log "ERROR: GCP TEE server at $TEE_HOST:$TEE_PORT is not reachable."
    log "Check that the VM is running and port 8000 is open."
    return 1
  fi
  log "  [OK] GCP TEE server is reachable."
  return 0
}

# Verify connection to an already-running TEE server at --host:--port
verify_tee_connect() {
  log "connect mode: verifying connection to $TEE_HOST:$TEE_PORT..."
  if ! timeout 5 bash -c "echo > /dev/tcp/$TEE_HOST/$TEE_PORT" >/dev/null 2>&1; then
    log "ERROR: TEE server at $TEE_HOST:$TEE_PORT is not reachable."
    return 1
  fi
  log "  [OK] TEE server is reachable."
  return 0
}

# Route to the correct TEE startup function
setup_tee_server() {
  log "TEE mode: $TEE_MODE, Verifier: $VERIFIER"
  if [[ "$TEE_MODE" == "gcp_discover" ]]; then
    start_tee_gcp_discover
  elif [[ "$TEE_MODE" == "connect" ]]; then
    verify_tee_connect
  else
    # Pre-flight cleanup for local runs
    log "Cleaning up any pre-existing local TEE processes..."
    fuser -k "$TEE_PORT/tcp" 2>/dev/null || true
    fuser -k "$TEE_LOCAL_PORT/tcp" 2>/dev/null || true
    pkill -x ztab_server 2>/dev/null || true
    docker rm -f ztab-server 2>/dev/null || true
    sleep 1

    if [[ "$TEE_MODE" == "docker_build" ]]; then
      local docker_creator_args=()
      if [[ -n "$CREATOR_TOKEN" ]]; then
        docker_creator_args=("--creator_token" "$CREATOR_TOKEN")
      fi
      start_tee_docker "$TEE_PORT" \
        --llm --gcs_bucket "$GCS_BUCKET" \
        --policy_dir "$ZTAB_DIR/examples/calendar/" \
        ${docker_creator_args[@]+"${docker_creator_args[@]}"}
    elif [[ "$TEE_MODE" == "local_build" ]]; then
      start_tee_native "$TEE_PORT"
    fi
  fi
}

# --- Phase 3: Build ---
build_binaries() {
  log "Verifying Python proto stubs..."
  local proto_file="$ZTAB_DIR/tee/session_manager.proto"
  local pb2_file="$ZTAB_DIR/agent/pb2/session_manager_pb2.py"
  local pb2_grpc="$ZTAB_DIR/agent/pb2/session_manager_pb2_grpc.py"

  if [[ ! -f "$pb2_file" || ! -f "$pb2_grpc" ]]; then
    log "ERROR: Generated python proto stubs are missing."
    log "Please run '$ZTAB_DIR/agent/regen_protos.sh' to generate them."
    return 1
  fi

  if [[ "$proto_file" -nt "$pb2_file" || "$proto_file" -nt "$pb2_grpc" ]]; then
    log "ERROR: Python proto stubs are out of date compared to $proto_file."
    log "Please regenerate them by running '$ZTAB_DIR/agent/regen_protos.sh'."
    return 1
  fi

  return 0
}

# --- Phase 4: Cold-Start Assertions ---
assert_cold_start() {
  local agent_num="$1"
  local agent_home="$RUN_DIR/agents/$agent_num/home"
  local mcp_config="$agent_home/.gemini/config/mcp_config.json"
  local backends_file="$agent_home/.ztab/backends.json"

  # 1. mcp_config.json must exist but must NOT contain "ztab"
  if grep -q '"ztab"' "$mcp_config" 2>/dev/null; then
    log "COLD-START VIOLATION [agent $agent_num]: $mcp_config already contains 'ztab'."
    exit 1
  fi

  # 2. backends.json must NOT exist
  if [[ -f "$backends_file" ]]; then
    log "COLD-START VIOLATION [agent $agent_num]: $backends_file already exists."
    exit 1
  fi

  # 3. No ztab virtualenv in this home
  if [[ -d "$agent_home/.ztab-venv" ]]; then
    log "COLD-START VIOLATION [agent $agent_num]: .ztab-venv already exists."
    exit 1
  fi

  log "  Agent $agent_num: cold-start OK"
}

run_cold_start_assertions() {
  if [[ -n "$REUSE_DIR" ]]; then
    log "REUSE MODE: Skipping cold-start assertions."
    return 0
  fi
  for i in $(seq 1 $NUM_AGENTS); do
    assert_cold_start "$i"
  done
  return 0
}

# --- Phase 5: Launch Per-Agent LS Instances ---
start_agent_ls() {
  local agent_num="$1"
  local agent_dir="$RUN_DIR/agents/$agent_num"
  local agent_home="$agent_dir/home"

  # B.3: Launch with random port (--http_server_port=0) instead of fixed port.
  # The LS will write a discovery file with the actual port.
  GEMINI_API_KEY="${GEMINI_API_KEY:-}" \
  AICORE_GEMINI_API_KEY="${AICORE_GEMINI_API_KEY:-}" \
  HOME="$agent_home" \
  $LS_BIN \
    --standalone=true \
    --http_server_port=0 \
    -persistent_mode \
    --csrf_token=$STATIC_TOKEN \
    --workspace_id="$WORKSPACE_ID" \
    --app_data_dir="$APP_DATA_DIR" \
    $LS_EXTRA_FLAGS \
    > "$agent_dir/ls.log" 2>&1 &
  AGENT_LS_PIDS[$agent_num]=$!
  log "  Agent $agent_num LS started with random port (PID ${AGENT_LS_PIDS[$agent_num]})"
}

start_all_ls() {
  log "Starting $NUM_AGENTS Language Server instance(s)..."
  for i in $(seq 1 $NUM_AGENTS); do
    start_agent_ls "$i"
  done

  # B.3: Discover ports via the LS discovery JSON files.
  # Each LS writes its random port to ~/.gemini/{app_data_dir}/daemon/ls_{hash}.json
  for i in $(seq 1 $NUM_AGENTS); do
    local agent_home="$RUN_DIR/agents/$i/home"
    local gemini_dir="$agent_home/.gemini/$APP_DATA_DIR"
    local discovered_port
    discovered_port=$(python3 -c "
import sys
sys.path.insert(0, '$(dirname "${BASH_SOURCE[0]}")')
from harness_lib import get_discovered_http_port
try:
    port = get_discovered_http_port('$WORKSPACE_ID', '$gemini_dir', timeout=30)
    print(port)
except Exception as e:
    print(f'ERROR: {e}', file=sys.stderr)
    sys.exit(1)
" 2>>"$RUN_DIR/agents/$i/ls.log")
    if [[ $? -ne 0 ]]; then
      log "ERROR: Agent $i port discovery failed. See $RUN_DIR/agents/$i/ls.log"
      tail -20 "$RUN_DIR/agents/$i/ls.log" >&2 || true
      return 1
    fi
    AGENT_PORTS[$i]="$discovered_port"
    log "  Agent $i LS ready on discovered port $discovered_port"
  done
  return 0
}

# --- Phase 6: Trigger ---
trigger_agents() {
  log "Triggering $NUM_AGENTS agent(s)..."
  SKILL_PATH="$ZTAB_DIR/agent/SKILL.md"

  # Build verifier flags for trigger scripts
  local verifier_flags="--verifier $VERIFIER"
  if [[ -n "$EXPECTED_DIGEST" ]]; then
    verifier_flags="$verifier_flags --expected_digest $EXPECTED_DIGEST"
  fi

  # Build comma-separated ports string
  local ports_csv=""
  for i in $(seq 1 $NUM_AGENTS); do
    if [[ -n "$ports_csv" ]]; then
      ports_csv="$ports_csv,${AGENT_PORTS[$i]}"
    else
      ports_csv="${AGENT_PORTS[$i]}"
    fi
  done

  local phase_flag=""
  if [[ $PHASE1_ONLY -eq 1 ]]; then
    phase_flag="--phase1_only"
  fi

  local creator_trigger_flag=""
  if [[ -n "$CREATOR_TOKEN" ]]; then
    creator_trigger_flag="--creator_token $CREATOR_TOKEN"
  fi

  TRIGGER_OUT=$(python3 "$SCRIPT_DIR/trigger_session_test.py" \
    --ports "$ports_csv" \
    --csrf_token "$STATIC_TOKEN" \
    --workspace "$WORKSPACE_DIR" \
    --host "$TEE_HOST" \
    --tee_port "$TEE_PORT" \
    --skill_path "$SKILL_PATH" \
    --model "$MODEL_NAME" \
    --ztab_dir "$ZTAB_DIR" \
    --tee_log "$RUN_DIR/tee_server.log" \
    --run_dir "$RUN_DIR" \
    --num_agents "$NUM_AGENTS" \
    $verifier_flags \
    $phase_flag \
    $creator_trigger_flag \
    2>&1) || true
  echo "$TRIGGER_OUT" > "$RUN_DIR/trigger.log"
  # Extract conversation IDs from trigger output (JSON on last line)
  # Phase 2 uses NEW cascades (StartCascade), so we extract both sets of IDs.
  # The monitor should track Phase 2 IDs (active cascades) when available.
  AGENT_IDS=$(echo "$TRIGGER_OUT" | python3 -c "
import sys, json
for line in reversed(sys.stdin.readlines()):
    line = line.strip()
    if line.startswith('{'):
        d = json.loads(line)
        ids = d.get('agent_conversation_ids', [])
        print(','.join(ids))
        break
" 2>/dev/null || true)
  PHASE2_IDS=$(echo "$TRIGGER_OUT" | python3 -c "
import sys, json
for line in reversed(sys.stdin.readlines()):
    line = line.strip()
    if line.startswith('{'):
        d = json.loads(line)
        ids = d.get('phase2_conversation_ids', [])
        if ids:
            print(','.join(ids))
        break
" 2>/dev/null || true)
  if [[ -z "$AGENT_IDS" ]]; then return 1; fi
  log "  Phase 1 IDs: $AGENT_IDS"
  if [[ -n "$PHASE2_IDS" ]]; then
    log "  Phase 2 IDs: $PHASE2_IDS"
  fi
  return 0
}

# --- Phase 7: Poll results ---
poll_results() {
  log "Polling results for $NUM_AGENTS agent(s)..."

  # Build comma-separated ports for monitor
  local ports_csv=""
  for i in $(seq 1 $NUM_AGENTS); do
    if [[ -n "$ports_csv" ]]; then
      ports_csv="$ports_csv,${AGENT_PORTS[$i]}"
    else
      ports_csv="${AGENT_PORTS[$i]}"
    fi
  done

  # Use Phase 2 IDs for monitoring if available (they're the active cascades).
  # Phase 2 creates new cascades with fresh executors, so the Phase 1 cascades
  # are now idle. The monitor needs to track Phase 2.
  local monitor_ids="$AGENT_IDS"
  if [[ -n "$PHASE2_IDS" ]]; then
    monitor_ids="$PHASE2_IDS"
    log "  Using Phase 2 cascade IDs for monitoring"
  fi

  # Use agent 1's venv python for the monitor — it needs grpc.
  local monitor_python="python3"
  local agent1_venv="$RUN_DIR/agents/1/home/.ztab-venv/bin/python3"
  if [[ -x "$agent1_venv" ]]; then
    monitor_python="$agent1_venv"
  fi

  "$monitor_python" "$SCRIPT_DIR/monitor_session_test.py" \
    --ports "$ports_csv" \
    --csrf_token "$STATIC_TOKEN" \
    --agent_ids "$monitor_ids" \
    --run_dir "$RUN_DIR" \
    --timeout 900 \
    --tee_host "$TEE_HOST" \
    --tee_port "$TEE_PORT" \
    --verifier "$VERIFIER"
  MONITOR_EXIT=$?
  if [[ $MONITOR_EXIT -ne 0 ]]; then
    log "  ERROR: Monitor exited with code $MONITOR_EXIT"
    return 1
  fi
  log "  SUCCESS: All $NUM_AGENTS agents finished successfully."
  return 0
}

# --- Phase 8: Post-Run Trajectory Audit ---
run_trajectory_audit() {
  log "Running trajectory audit for $NUM_AGENTS agent(s)..."
  local audit_phase_flag=""
  local audit_reuse_flag=""
  local audit_verifier_flag="--expected_verifier $VERIFIER"
  if [[ $PHASE1_ONLY -eq 1 ]]; then
    audit_phase_flag="--phase1_only"
  fi
  if [[ -n "$REUSE_DIR" ]]; then
    audit_reuse_flag="--reuse_run --expected_host $TEE_HOST --expected_port $TEE_PORT"
  fi
  local audit_creator_flag=""
  if [[ -n "$CREATOR_TOKEN" ]]; then
    audit_creator_flag="--creator_token $CREATOR_TOKEN"
  fi
  set +o pipefail
  python3 "$SCRIPT_DIR/audit_trajectory.py" \
    --run_dir "$RUN_DIR" \
    --num_agents "$NUM_AGENTS" \
    --tee_mode "$TEE_MODE" \
    $audit_phase_flag \
    $audit_reuse_flag \
    $audit_verifier_flag \
    $audit_creator_flag \
    2>&1 | tee -a "$RUN_DIR/audit.log"
  AUDIT_EXIT=${PIPESTATUS[0]}
  set -o pipefail
  if [[ $AUDIT_EXIT -ne 0 ]]; then
    if [[ $AUDIT_NON_BLOCKING -eq 1 ]]; then
      log "  WARNING: Trajectory audit reported failures (exit=$AUDIT_EXIT) [non-blocking]"
    else
      log "  FAIL: Trajectory audit reported failures (exit=$AUDIT_EXIT)"
      AUDIT_FAILED=1
    fi
    log "  See $RUN_DIR/audit.log and per-agent audit.json files"
  fi
  return 0
}

# --- Main Execution ---
phase "Setup Environment" setup_env "$@"
phase "Start TEE Server" setup_tee_server
phase "Build Binaries" build_binaries
phase "Cold-Start Assertions" run_cold_start_assertions
phase "Start LS (per-agent)" start_all_ls
phase "Trigger Agents" trigger_agents
if [[ $PHASE1_ONLY -eq 0 ]]; then
  phase "Poll Results" poll_results
fi
phase "Trajectory Audit" run_trajectory_audit

# --- D-series: Dirty State Battery ---
if [[ $DIRTY_BATTERY -eq 1 ]]; then
  run_dirty_state_battery() {
    log "=== D-SERIES: DIRTY STATE BATTERY ==="
    log "Using run dir $RUN_DIR as baseline (state from prior run)"

    local d_pass=0
    local d_fail=0
    local d_total=0
    local expected_verifier="$VERIFIER"

    # Helper: run a single D-test
    run_d_test() {
      local d_id="$1"
      local d_desc="$2"
      shift 2
      # $@ = mutation commands

      d_total=$((d_total + 1))
      log ""
      log "--- D${d_id}: ${d_desc} ---"

      # Apply mutations
      "$@"
      local mutation_exit=$?
      if [[ $mutation_exit -ne 0 ]]; then
        log "  ✘ D${d_id}: FAIL (mutation command failed with exit=$mutation_exit)"
        d_fail=$((d_fail + 1))
        return
      fi

      # Re-trigger Phase 1 only (reuse existing sandbox)
      log "  Triggering Phase 1 re-run..."
      local trigger_flags=""
      trigger_flags="--ports $(IFS=,; echo "${AGENT_PORTS[*]}")"
      trigger_flags="$trigger_flags --csrf_token $STATIC_TOKEN"
      trigger_flags="$trigger_flags --workspace $WORKSPACE_DIR"
      trigger_flags="$trigger_flags --host $TEE_HOST --tee_port $TEE_PORT"
      trigger_flags="$trigger_flags --skill_path ${ZTAB_DIR}/agent/SKILL.md"
      trigger_flags="$trigger_flags --model $MODEL_NAME"
      trigger_flags="$trigger_flags --run_dir $RUN_DIR"
      trigger_flags="$trigger_flags --num_agents $NUM_AGENTS"
      trigger_flags="$trigger_flags --ztab_dir $ZTAB_DIR"
      trigger_flags="$trigger_flags --phase1_only"
      trigger_flags="$trigger_flags --verifier $VERIFIER"
      python3 "$SCRIPT_DIR/trigger_session_test.py" $trigger_flags 2>&1 | \
        tee -a "$RUN_DIR/d${d_id}.log" || true

      # Run audit with --reuse_run
      log "  Running reuse-run audit..."
      local d_creator_flag=""
      if [[ -n "$CREATOR_TOKEN" ]]; then
        d_creator_flag="--creator_token $CREATOR_TOKEN"
      fi
      python3 "$SCRIPT_DIR/audit_trajectory.py" \
        --run_dir "$RUN_DIR" \
        --num_agents "$NUM_AGENTS" \
        --tee_mode "$TEE_MODE" \
        --phase1_only \
        --reuse_run \
        --expected_host "$TEE_HOST" \
        --expected_port "$TEE_PORT" \
        --expected_verifier "$expected_verifier" \
        $d_creator_flag \
        2>&1 | tee -a "$RUN_DIR/d${d_id}_audit.log" || true
      local d_exit=${PIPESTATUS[0]}

      if [[ $d_exit -eq 0 ]]; then
        log "  ✔ D${d_id}: PASS"
        d_pass=$((d_pass + 1))
      else
        log "  ✘ D${d_id}: FAIL"
        d_fail=$((d_fail + 1))
      fi
    }

    # --- D1: Clean re-run (no mutations) ---
    run_d_test 1 "Clean re-run (no mutations)" true

    # --- D2: Wrong backend host ---
    run_d_test 2 "Wrong backend host recovery" \
      bash -c '
        for i in $(seq 1 '$NUM_AGENTS'); do
          bp="'$RUN_DIR'/agents/$i/home/.ztab/backends.json"
          if [[ -f "$bp" ]]; then
            python3 -c "import json,sys; d=json.load(open(sys.argv[1])); [b.__setitem__(\"host\",\"10.0.0.1\") for b in d.get(\"backends\",[])] ; json.dump(d,open(sys.argv[1],\"w\"),indent=2)" "$bp"
          fi
        done
      '

    # --- D3: Wrong verifier ---
    run_d_test 3 "Wrong verifier recovery" \
      bash -c '
        for i in $(seq 1 '$NUM_AGENTS'); do
          bp="'$RUN_DIR'/agents/$i/home/.ztab/backends.json"
          if [[ -f "$bp" ]]; then
            python3 -c "import json,sys; d=json.load(open(sys.argv[1])); [b.__setitem__(\"verifier\",\"BOGUS\") for b in d.get(\"backends\",[])] ; json.dump(d,open(sys.argv[1],\"w\"),indent=2)" "$bp"
          fi
        done
      '

    # --- D4: Corrupted venv (delete key file) ---
    run_d_test 4 "Corrupted venv recovery" \
      bash -c '
        for i in $(seq 1 '$NUM_AGENTS'); do
          home="'$RUN_DIR'/agents/$i/home"
          mcp="$home/.gemini/config/mcp_config.json"
          if [[ -f "$mcp" ]]; then
            py=$(python3 -c "import json; print(json.load(open(\"$mcp\")).get(\"mcpServers\",{}).get(\"ztab\",{}).get(\"command\",\"\"))")
            if [[ -n "$py" && -f "$py" ]]; then
              rm -f "$py"
            fi
          fi
        done
      '

    # --- D5: Stale config path ---
    run_d_test 5 "Stale mcp_config path recovery" \
      bash -c '
        for i in $(seq 1 '$NUM_AGENTS'); do
          mcp="'$RUN_DIR'/agents/$i/home/.gemini/config/mcp_config.json"
          if [[ -f "$mcp" ]]; then
            python3 -c "import json,sys; d=json.load(open(sys.argv[1])); d.get(\"mcpServers\",{}).get(\"ztab\",{}).__setitem__(\"command\",\"/tmp/nonexistent/python3\"); json.dump(d,open(sys.argv[1],\"w\"),indent=2)" "$mcp"
          fi
        done
      '

    # --- D6: Missing venv (delete venv dir) ---
    run_d_test 6 "Missing venv recovery" \
      bash -c '
        for i in $(seq 1 '$NUM_AGENTS'); do
          home="'$RUN_DIR'/agents/$i/home"
          rm -rf "$home/.ztab-venv" "$home/.ztab/venv" 2>/dev/null || true
        done
      '

    log ""
    log "=== D-SERIES RESULTS: $d_pass/$d_total PASS, $d_fail FAIL ==="
    if [[ $d_fail -gt 0 ]]; then
      AUDIT_FAILED=1
    fi
  }
  phase "Dirty State Battery" run_dirty_state_battery
fi

# --- Summary ---
if [[ $AUDIT_FAILED -eq 1 ]]; then
  FINAL_RESULT="FAIL"
else
  FINAL_RESULT="PASS"
fi
{
  echo "RESULT: $FINAL_RESULT"
  echo "TEE_MODE: $TEE_MODE"
  echo "VERIFIER: $VERIFIER"
  echo "AGENTS: $NUM_AGENTS"
  echo "TEE: $TEE_HOST:$TEE_PORT"
  if [[ "$TEE_MODE" == "local_build" ]]; then
    echo "MODEL: $MODEL_PATH"
  elif [[ "$TEE_MODE" == "docker_build" ]]; then
    echo "GCS_BUCKET: $GCS_BUCKET"
  fi
  echo "Logs: $RUN_DIR"
} > "$RUN_DIR/summary.txt"
log "All phases complete. Summary: $RUN_DIR/summary.txt"
