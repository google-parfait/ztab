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

Validates a VerifierPolicy and returns the corresponding
verifier callable. All safety-critical gates
(ZTAB_TEST_ENVIRONMENT checks for noop, allow_debug,
allow_memory_monitoring) are enforced inside each policy's
validate() method — this factory has no type-specific logic.
"""

from typing import Callable

from verifier_policy import VerifierPolicy


def get_verifier(
    policy: VerifierPolicy,
) -> Callable[[str, bytes], bool]:
    """Validate a policy and return its verifier callable.

    Args:
        policy: A VerifierPolicy subclass instance
            (NoopPolicy, ItaPolicy, etc.).

    Returns:
        A verifier callable
        (token: str, cert_pem: bytes) -> bool.

    Raises:
        ValueError: If the policy is internally
            inconsistent (e.g. missing digests).
        RuntimeError: If a safety bypass is active
            outside ZTAB_TEST_ENVIRONMENT=1.
    """
    policy.validate()
    return policy.create_verifier()
