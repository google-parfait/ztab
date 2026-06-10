# ZTAB Server Build & Run Container
#
# Two-stage build:
#   1. Builder stage: compiles the server with Bazel using a known-good toolchain.
#   2. Runtime stage: minimal image with just the binary.
#
# Build from the tee/ directory:
#   docker build -t ztab-server .
#
# Run:
#   docker run -p 8000:8000 ztab-server

# ---- Builder Stage ----
FROM ubuntu:24.04 AS builder

ENV DEBIAN_FRONTEND=noninteractive

# Install build essentials and Bazelisk.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    git \
    python3 \
    && rm -rf /var/lib/apt/lists/*

# Install Bazelisk (auto-downloads the right Bazel version from .bazelversion).
RUN curl -fsSL -o /usr/local/bin/bazel \
    https://github.com/bazelbuild/bazelisk/releases/download/v1.25.0/bazelisk-linux-amd64 \
    && chmod +x /usr/local/bin/bazel

WORKDIR /build

# Copy everything in tee/ (the Docker context is tee/).
COPY . ./

# Build the server binary.
RUN bazel build //:ztab_server --jobs=auto

# ---- Runtime Stage ----
FROM ubuntu:24.04 AS runtime

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /build/bazel-bin/ztab_server /usr/local/bin/ztab_server

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/ztab_server"]
CMD ["8000"]
