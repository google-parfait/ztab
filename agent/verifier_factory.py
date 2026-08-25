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

import os
import sys
import warnings

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
        RuntimeError: If 'noop' is selected outside of test environment.
        SystemExit: If the verifier name is unknown (cli mode).
    """
    if name == "noop":
        if os.environ.get("ZTAB_TEST_ENVIRONMENT") != "1":
            raise RuntimeError(
                "Verifier 'noop' is strictly forbidden outside test "
                "environments. Set ZTAB_TEST_ENVIRONMENT=1 in test harnesses, "
                "or use verifier 'ita' with --expected-digest for production."
            )
        warnings.warn(
            "Using noop verifier: NO attestation validation will be "
            "performed. Any server certificate will be accepted. This is "
            "only appropriate for local testing.",
            stacklevel=2,
        )
        return noop_verifier
    elif name == "ita":
        if not expected_digest:
            raise ValueError(
                "Verifier 'ita' requires an expected container image digest. "
                "Pass --expected-digest sha256:... or configure expected_digest in backends.json."
            )
        from ita_verifier import create_ita_verifier
        return create_ita_verifier(
            expected_image_digest=expected_digest,
            require_debug_disabled=not allow_debug,
        )
    else:
        print(f"Unknown verifier: '{name}'. Use 'noop' or 'ita'.",
              file=sys.stderr)
        sys.exit(1)
