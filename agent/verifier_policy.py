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

"""Polymorphic attestation verification policies for ZTAB.

Defines a VerifierPolicy ABC with concrete subclasses for
each supported verifier type (NoopPolicy, ItaPolicy). Each
policy encapsulates:

  - The verifier type name.
  - All type-specific configuration fields.
  - Self-validation (including ZTAB_TEST_ENVIRONMENT gates).
  - Serialization to/from dict (JSON-compatible).
  - Verifier callable creation.

Adding a new verifier type requires:
  1. Define a new VerifierPolicy subclass.
  2. Register it in _policy_registry().
  No changes needed in verifier_factory.py, cli.py, or
  mcp_server.py.

Design rationale — what lives WHERE:

  Fixed constants (issuer, hwmodel, swname, audience,
  cvm_compliance, secure_boot) are hardcoded in
  ita_verifier.py because they are inherent to the
  ITA / Confidential Space protocol. Any legitimate CS
  TEE always has these values. If you are using ITA,
  these are unconditionally enforced — there is no
  toggle.

  The policy only exposes fields the caller legitimately
  needs to configure:
  - Container identity (digests, project, SA).
  - Safety bypasses that correspond to real TEE modes
    (debug images, monitoring), gated by test env.
  - Version floor and clock skew.
"""

import abc
import dataclasses
import os
from typing import Callable, Dict, FrozenSet


# Minimum Confidential Space image version that
# supports ITA attestation (2026-05-00 release).
# See: https://cloud.google.com/confidential-computing/
#      confidential-space/docs/release-notes
_MIN_CS_VERSION_DEFAULT = 260500


class VerifierPolicy(abc.ABC):
    """Base class for attestation verification policies.

    Each subclass represents a verifier type (ITA, noop,
    future GCA, etc.) and encapsulates its own fields,
    validation, serialization, and verifier creation.
    """

    @property
    @abc.abstractmethod
    def verifier_type(self) -> str:
        """Short name: 'ita', 'noop', etc."""

    @abc.abstractmethod
    def validate(self) -> None:
        """Raise ValueError/RuntimeError if invalid.

        This includes ZTAB_TEST_ENVIRONMENT gates for
        safety-critical bypasses.
        """

    @abc.abstractmethod
    def create_verifier(
        self,
    ) -> Callable[[str, bytes], bool]:
        """Return the verifier callable."""

    @abc.abstractmethod
    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict."""

    @classmethod
    def from_dict(cls, d: dict) -> "VerifierPolicy":
        """Deserialize from a JSON-compatible dict.

        Dispatches to the correct subclass based on
        'verifier_type'.
        """
        vtype = d.get("verifier_type", "")
        registry = _policy_registry()
        if vtype not in registry:
            raise ValueError(
                f"Unknown verifier_type '{vtype}'. "
                f"Known types: "
                f"{sorted(registry.keys())}"
            )
        return registry[vtype].from_dict(d)


@dataclasses.dataclass(frozen=True)
class NoopPolicy(VerifierPolicy):
    """Test-only policy: no attestation verification.

    Validation enforces that ZTAB_TEST_ENVIRONMENT=1;
    noop is never permitted in production.
    """

    @property
    def verifier_type(self) -> str:
        return "noop"

    def validate(self) -> None:
        if (
            os.environ.get("ZTAB_TEST_ENVIRONMENT")
            != "1"
        ):
            raise RuntimeError(
                "noop verifier forbidden outside "
                "ZTAB_TEST_ENVIRONMENT=1."
            )

    def create_verifier(self):
        from client import noop_verifier

        return noop_verifier

    def to_dict(self) -> dict:
        return {"verifier_type": "noop"}

    @classmethod
    def from_dict(cls, d: dict) -> "NoopPolicy":
        return cls()


@dataclasses.dataclass(frozen=True)
class ItaPolicy(VerifierPolicy):
    """ITA (Intel Trust Authority) attestation policy.

    Contains only fields the caller legitimately needs
    to configure. Protocol-fixed invariants (issuer,
    hwmodel, swname, audience, cvm_compliance,
    secure_boot) are unconditional checks hardcoded
    in ita_verifier.py — no toggles.

    The only bypasses are for real TEE modes that a
    test might need: debug images and memory
    monitoring. Both are gated by
    ZTAB_TEST_ENVIRONMENT=1.
    """

    # --- Container identity (required) ---
    expected_image_digests: FrozenSet[str] = (
        frozenset()
    )

    # --- Optional GCP identity enrichment ---
    # Empty string = skip the check.
    expected_project_id: str = ""
    expected_service_account: str = ""

    # --- Version floor ---
    # Required > 0. Default is the first CS release
    # that supports ITA (2026-05-00).
    min_cs_version: int = _MIN_CS_VERSION_DEFAULT

    # --- Safety bypasses (test-env gated) ---
    # A real CS TEE can run in debug mode (preview
    # images) or with memory monitoring enabled.
    # These bypasses let tests connect to such TEEs.
    # Setting either to True is forbidden outside
    # ZTAB_TEST_ENVIRONMENT=1.
    allow_debug: bool = False
    allow_memory_monitoring: bool = False

    # --- JWT ---
    clock_skew_seconds: int = 300

    @property
    def verifier_type(self) -> str:
        return "ita"

    def validate(self) -> None:
        # --- Hard requirements ---
        if not self.expected_image_digests:
            raise ValueError(
                "expected_image_digests must contain "
                "at least one digest "
                "(e.g. 'sha256:...')."
            )
        for d in self.expected_image_digests:
            if not d.startswith("sha256:"):
                raise ValueError(
                    f"Digest '{d}' does not start "
                    f"with 'sha256:'. All digests "
                    f"must use the 'sha256:' prefix."
                )

        if self.min_cs_version <= 0:
            raise ValueError(
                "min_cs_version must be a positive "
                "integer (e.g. 260500). Got: "
                f"{self.min_cs_version}"
            )

        # --- Test-environment gates ---
        _is_test = (
            os.environ.get("ZTAB_TEST_ENVIRONMENT")
            == "1"
        )

        if self.allow_debug and not _is_test:
            raise RuntimeError(
                "allow_debug=True forbidden outside "
                "ZTAB_TEST_ENVIRONMENT=1."
            )

        if (
            self.allow_memory_monitoring
            and not _is_test
        ):
            raise RuntimeError(
                "allow_memory_monitoring=True "
                "forbidden outside "
                "ZTAB_TEST_ENVIRONMENT=1."
            )

    def create_verifier(self):
        from ita_verifier import create_ita_verifier

        return create_ita_verifier(self)

    def to_dict(self) -> dict:
        d = {"verifier_type": "ita"}
        d["expected_image_digests"] = sorted(
            self.expected_image_digests
        )
        # Serialize fields that differ from defaults.
        for field in dataclasses.fields(self):
            if field.name == "expected_image_digests":
                continue  # Already handled above.
            val = getattr(self, field.name)
            default = field.default
            if (
                default
                is dataclasses.MISSING
            ):
                continue
            if val != default:
                d[field.name] = val
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ItaPolicy":
        digests = d.get(
            "expected_image_digests", []
        )
        if isinstance(digests, str):
            digests = [digests]
        kwargs: Dict = {
            "expected_image_digests": frozenset(
                digests
            ),
        }
        # Map all known fields from the dict,
        # casting int fields to prevent TypeError
        # from loosely-typed JSON payloads.
        _INT_FIELDS = {
            "min_cs_version",
            "clock_skew_seconds",
        }
        for field in dataclasses.fields(cls):
            if field.name == "expected_image_digests":
                continue  # Already handled above.
            if field.name in d:
                val = d[field.name]
                if field.name in _INT_FIELDS:
                    if val is None or val == "":
                        continue  # Use default.
                    val = int(val)
                kwargs[field.name] = val
        return cls(**kwargs)


def _policy_registry() -> Dict[str, type]:
    """Map of verifier_type -> policy class.

    Register new verifier types here.
    """
    return {
        "noop": NoopPolicy,
        "ita": ItaPolicy,
        # Future: "gca": GcaPolicy,
    }
