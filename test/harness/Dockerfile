# ==============================================================================
# ZTAB Test Harness Docker Image
# ==============================================================================
# Builds a self-contained image with the harness scripts, Python dependencies,
# and the public Antigravity Language Server binary (downloaded at build time).
#
# Build:
#   docker build -t ztab-harness:oss -f test/harness/Dockerfile .
#
# Run (external TEE):
#   docker run --rm --net=host \
#     --user \$(id -u):\$(id -g) \
#     -e HOME=/home/user \
#     -v ~/.gemini:/home/user/.gemini:ro \
#     -v /tmp/ztab_runs:/tmp/ztab_runs \
#     ztab-harness:oss \
#     --ztab_dir /ztab \
#     --num_agents 1 --tee_mode connect \
#     --model MODEL_GOOGLE_GEMINI_2_5_FLASH \
#     --ls_extra_flags "--cloud_code_endpoint=https://cloudcode-pa.googleapis.com"
# ==============================================================================

FROM debian:bookworm-slim

# --- Configurable LS version and execution ID ---
# Override at build time: docker build --build-arg LS_VERSION=2.0.11 --build-arg LS_EXEC_ID=6560309696135168 ...
ARG LS_VERSION=2.0.11
ARG LS_EXEC_ID=6560309696135168

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    python3-venv \
    curl \
    jq \
    procps \
    docker.io \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies for the Connect RPC client
RUN pip3 install --break-system-packages \
    protobuf \
    grpcio-tools \
    requests \
    cryptography \
    pyjwt

# --- Download and extract the public Antigravity Language Server ---
# Uses AppImage --appimage-extract to avoid FUSE requirement in containers.
# The extracted binary lands at /usr/local/bin/language_server.
RUN set -ex \
    && curl -fsSL -o /tmp/Antigravity.AppImage \
       "https://storage.googleapis.com/antigravity-public/antigravity-hub/${LS_VERSION}-${LS_EXEC_ID}/linux-x64/Antigravity.AppImage" \
    && chmod +x /tmp/Antigravity.AppImage \
    && cd /tmp && ./Antigravity.AppImage --appimage-extract \
    && cp /tmp/squashfs-root/resources/bin/language_server /usr/local/bin/language_server \
    && chmod +x /usr/local/bin/language_server \
    && rm -rf /tmp/Antigravity.AppImage /tmp/squashfs-root

# Copy the ZTAB repository (filtered by .dockerignore)
WORKDIR /ztab
COPY . .

# Make harness scripts executable
RUN chmod +x test/harness/test_cold_start.sh \
    && chmod +x gcp/launch.sh 2>/dev/null || true

# Set up environment.
# Pre-create /home/user with open permissions so that if an agent process
# resolves $HOME to this path (instead of the per-agent sandbox), file
# writes do not fail with Permission denied.
RUN mkdir -p /home/user && chmod 777 /home/user
ENV HOME="/home/user"
ENV PYTHONPATH="/ztab"
ENV PATH="/ztab/test/harness:${PATH}"

# The entrypoint delegates to the harness script
ENTRYPOINT ["/ztab/test/harness/test_cold_start.sh"]
