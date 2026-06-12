#!/usr/bin/env python3
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

"""Generate Python proto stubs from session_manager.proto.

Run this once (or whenever the proto changes) from the agent/ directory:
    python3 generate_protos.py

Requires grpcio-tools: pip install grpcio-tools
"""

import subprocess
import sys
import os

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROTO_DIR = os.path.join(os.path.dirname(SCRIPT_DIR), "tee")
PROTO_FILE = os.path.join(PROTO_DIR, "session_manager.proto")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "pb2")


def main():
    if not os.path.isfile(PROTO_FILE):
        print(f"ERROR: Proto file not found: {PROTO_FILE}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # Design Decision: While pb2/__init__.py is checked into the repository making
    # this generation technically redundant, it serves as an intentional safety fallback
    # in case the directory is ever cleared or ignored locally during development.
    init_file = os.path.join(OUTPUT_DIR, "__init__.py")
    if not os.path.exists(init_file):
        with open(init_file, "w") as f:
            f.write("# Auto-generated package marker.\n")

    cmd = [
        sys.executable,
        "-m",
        "grpc_tools.protoc",
        f"-I={PROTO_DIR}",
        f"--python_out={OUTPUT_DIR}",
        f"--grpc_python_out={OUTPUT_DIR}",
        PROTO_FILE,
    ]
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        print(
            "ERROR: Proto compilation failed. "
            "Make sure grpcio-tools is installed: pip install grpcio-tools",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Generated stubs in {OUTPUT_DIR}:")
    for f in sorted(os.listdir(OUTPUT_DIR)):
        if f.endswith("_pb2.py") or f.endswith("_pb2_grpc.py"):
            print(f"  {f}")
    print("Done.")


if __name__ == "__main__":
    main()
