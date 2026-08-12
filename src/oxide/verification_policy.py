"""Oxide-owned verification policy identity and prompt material."""

from __future__ import annotations

import hashlib
from pathlib import Path

POLICY_PROFILE = "pervasive-verus-v1"
POLICY_SCHEMA = 1
_REPOSITORY_POLICY_PATH = Path(__file__).resolve().parents[2] / "docs" / "VERIFICATION_PRIMER.md"
_PACKAGED_POLICY_PATH = Path(__file__).with_name("VERIFICATION_PRIMER.md")
POLICY_PATH = (
    _REPOSITORY_POLICY_PATH if _REPOSITORY_POLICY_PATH.is_file() else _PACKAGED_POLICY_PATH
)
_RATIONALE_HEADING = "\n## Engineering rationale\n"


def verification_policy_bytes() -> bytes:
    return POLICY_PATH.read_bytes()


def verification_policy_text() -> str:
    return verification_policy_bytes().decode("utf-8")


def verification_policy_normative_text() -> str:
    """Return only the normative portion injected into target-facing agent turns."""
    text = verification_policy_text()
    if _RATIONALE_HEADING not in text:
        raise RuntimeError("Oxide verification policy has no normative/rationale boundary")
    return text.split(_RATIONALE_HEADING, 1)[0]


def verification_policy_digest() -> str:
    return "sha256:" + hashlib.sha256(verification_policy_bytes()).hexdigest()


def verification_policy_prompt() -> str:
    """Return the exact normative policy as an explicitly non-product prompt input."""
    digest = verification_policy_digest()
    return (
        "===== BEGIN OXIDE NORMATIVE VERIFICATION POLICY "
        f"profile={POLICY_PROFILE} schema={POLICY_SCHEMA} sha256={digest} =====\n"
        + verification_policy_normative_text().rstrip()
        + "\n===== END OXIDE NORMATIVE VERIFICATION POLICY ====="
    )
