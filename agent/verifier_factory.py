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

"""Shared verifier factory for ZTAB client components.

Centralizes the verifier selection logic so that cli.py and mcp_server.py
don't duplicate it.
"""

import sys

from client import noop_verifier


def get_verifier(name: str, expected_digest: str = None, allow_debug: bool = False):
    """Return a verifier function by name.

    Args:
        name: Verifier name ('noop' or 'ita').
        expected_digest: Optional expected container image digest for ITA.
        allow_debug: If True, allow TEEs with debug mode enabled.

    Returns:
        A verifier callable (token: str, cert_pem: bytes) -> bool.

    Raises:
        SystemExit: If the verifier name is unknown (cli mode).
    """
    if name == "noop":
        return noop_verifier
    elif name == "ita":
        from ita_verifier import create_ita_verifier
        return create_ita_verifier(
            expected_image_digest=expected_digest,
            require_debug_disabled=not allow_debug,
        )
    else:
        print(f"Unknown verifier: '{name}'. Use 'noop' or 'ita'.",
              file=sys.stderr)
        sys.exit(1)
