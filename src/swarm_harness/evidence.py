from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any


class EvidenceError(RuntimeError):
    """A persisted check receipt is malformed or does not match its requirement."""


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def evidence_key(requirement: dict[str, Any]) -> str:
    return sha256_bytes(canonical_bytes(requirement))


def validate_declared_json_receipt(
    path: Path,
    *,
    maximum_bytes: int,
) -> dict[str, Any]:
    """Load one bounded, regular JSON-object receipt emitted by a checker."""
    try:
        metadata = path.lstat()
    except OSError as error:
        raise EvidenceError("required checker receipt is missing") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise EvidenceError("required checker receipt is not a regular file")
    if metadata.st_size > maximum_bytes:
        raise EvidenceError(f"required checker receipt exceeds {maximum_bytes} bytes")
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EvidenceError("required checker receipt is not valid JSON") from error
    if not isinstance(loaded, dict):
        raise EvidenceError("required checker receipt must be a JSON object")
    return loaded


def observed_environment() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "shell": "/bin/zsh",
    }


def _atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: object) -> bytes:
    encoded = canonical_bytes(value) + b"\n"
    _atomic_bytes(path, encoded)
    return encoded


def _artifact_ref(
    source: Path,
    destination: Path,
    *,
    kind: str,
    maximum_bytes: int,
) -> dict[str, Any]:
    size = source.stat().st_size
    digest = sha256_file(source)
    stored = size <= maximum_bytes
    relative: str | None = None
    if stored:
        suffix = re_suffix(kind)
        target = destination / "artifacts" / f"{digest.removeprefix('sha256:')}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if sha256_file(target) != digest:
                raise EvidenceError(f"artifact digest collision at {target}")
        else:
            shutil.copyfile(source, target)
        relative = target.relative_to(destination).as_posix()
    return {
        "kind": kind,
        "sha256": digest,
        "bytes": size,
        "stored": stored,
        "path": relative,
    }


def re_suffix(kind: str) -> str:
    safe = "".join(character if character.isalnum() else "-" for character in kind).strip("-")
    return "." + (safe or "artifact")


def begin_attempt(root: Path, requirement: dict[str, Any], attempt: str) -> None:
    key = evidence_key(requirement)
    _atomic_json(
        root / "attempts" / f"{attempt}.json",
        {
            "schema": "SwarmCheckAttemptV1",
            "state": "running",
            "evidence_key": key,
            "requirement": requirement,
            "execution_attempt": attempt,
            "started_at": time.time(),
        },
    )


def finish_attempt(
    root: Path,
    requirement: dict[str, Any],
    attempt: str,
    *,
    result: str,
    exit_code: int | None,
    stdout: Path,
    stderr: Path,
    artifact_paths: Iterable[Path] = (),
    maximum_artifact_bytes: int,
    maximum_artifacts: int = 128,
    started_at: float,
) -> tuple[dict[str, Any], str]:
    if result not in {"passed", "product_failure", "infrastructure_failure"}:
        raise EvidenceError(f"invalid check result classification: {result}")
    key = evidence_key(requirement)
    artifacts = [
        _artifact_ref(
            stdout,
            root,
            kind="stdout",
            maximum_bytes=maximum_artifact_bytes,
        ),
        _artifact_ref(
            stderr,
            root,
            kind="stderr",
            maximum_bytes=maximum_artifact_bytes,
        ),
    ]
    extras = sorted({path.resolve() for path in artifact_paths if path.is_file()})
    if len(extras) > maximum_artifacts:
        raise EvidenceError(
            f"check produced too many declared artifacts: {len(extras)} > {maximum_artifacts}"
        )
    for path in extras:
        artifacts.append(
            _artifact_ref(
                path,
                root,
                kind="declared-" + path.name,
                maximum_bytes=maximum_artifact_bytes,
            )
        )
    if any(not artifact["stored"] for artifact in artifacts):
        result = "infrastructure_failure"
    receipt = {
        "schema": "SwarmCheckEvidenceV1",
        "evidence_key": key,
        "requirement": requirement,
        "execution_attempt": attempt,
        "result": result,
        "exit_code": exit_code,
        "started_at": started_at,
        "completed_at": time.time(),
        "environment": observed_environment(),
        "artifacts": artifacts,
    }
    encoded = canonical_bytes(receipt) + b"\n"
    receipt_digest = sha256_bytes(encoded)
    receipt_path = root / "receipts" / f"{receipt_digest.removeprefix('sha256:')}.json"
    if receipt_path.exists():
        if receipt_path.read_bytes() != encoded:
            raise EvidenceError(f"receipt digest collision at {receipt_path}")
    else:
        _atomic_bytes(receipt_path, encoded)
    _atomic_json(
        root / "attempts" / f"{attempt}.json",
        {
            "schema": "SwarmCheckAttemptV1",
            "state": "completed",
            "evidence_key": key,
            "execution_attempt": attempt,
            "receipt_sha256": receipt_digest,
            "result": result,
        },
    )
    if result in {"passed", "product_failure"}:
        _atomic_json(
            root / "by-key" / f"{key.removeprefix('sha256:')}.json",
            {
                "schema": "SwarmCheckEvidencePointerV1",
                "evidence_key": key,
                "receipt_sha256": receipt_digest,
            },
        )
    return receipt, receipt_digest


def load_terminal_receipt(
    root: Path,
    requirement: dict[str, Any],
    *,
    execution_attempt: str | None = None,
) -> tuple[dict[str, Any], str] | None:
    key = evidence_key(requirement)
    pointer_path = root / "by-key" / f"{key.removeprefix('sha256:')}.json"
    if not pointer_path.is_file():
        return None
    try:
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        digest = str(pointer["receipt_sha256"])
        receipt_path = root / "receipts" / f"{digest.removeprefix('sha256:')}.json"
        encoded = receipt_path.read_bytes()
        receipt = json.loads(encoded)
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise EvidenceError("terminal check receipt is unreadable") from error
    if (
        pointer.get("schema") != "SwarmCheckEvidencePointerV1"
        or pointer.get("evidence_key") != key
        or not digest.startswith("sha256:")
        or sha256_bytes(encoded) != digest
        or receipt.get("schema") != "SwarmCheckEvidenceV1"
        or receipt.get("evidence_key") != key
        or receipt.get("requirement") != requirement
        or receipt.get("result") not in {"passed", "product_failure"}
    ):
        raise EvidenceError("terminal check receipt does not match its exact requirement")
    if execution_attempt is not None and receipt.get("execution_attempt") != execution_attempt:
        return None
    return receipt, digest


def artifact_digest(receipt: dict[str, Any], kind: str) -> str:
    for artifact in receipt.get("artifacts", []):
        if isinstance(artifact, dict) and artifact.get("kind") == kind:
            return str(artifact.get("sha256", ""))
    raise EvidenceError(f"receipt lacks {kind} artifact")
