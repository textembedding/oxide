"""Stable, fail-closed identity for one planning-prompt evaluation campaign."""

from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import os
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

MANIFEST_NAME = "evaluation-manifest.json"
MANIFEST_SCHEMA = "OxidePlanningEvaluationV1"


class EvaluationIdentityError(RuntimeError):
    """A run directory cannot be proven to belong to this evaluation."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _relative(repository: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(repository.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _files(repository: Path, paths: Iterable[Path]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for path in sorted({item.resolve() for item in paths}, key=lambda item: str(item)):
        result.append(
            {
                "path": _relative(repository, path),
                "sha256": _sha256(path.read_bytes()),
            }
        )
    return result


def corpus_files(cases: Iterable[object]) -> list[Path]:
    """Return every byte-bearing fixture input used by the selected cases."""
    paths: set[Path] = set()
    for case in cases:
        scenarios = (case.base, case.variant)
        case_directory = Path(case.base.directory).parent
        paths.add(case_directory / "example.toml")
        for scenario in scenarios:
            directory = Path(scenario.directory)
            paths.add(directory / "rubric.toml")
            paths.add(directory / "model-free-output.md")
            paths.update((directory / "specs").rglob("*.md"))
    missing = [path for path in paths if not path.is_file()]
    if missing:
        raise EvaluationIdentityError(
            "evaluation input disappeared while computing its identity: "
            + ", ".join(str(path) for path in sorted(missing))
        )
    return sorted(paths)


def _callable_code_identity(value: Callable[..., object]) -> dict[str, str]:
    identity = {
        "kind": f"{value.__module__}.{getattr(value, '__qualname__', type(value).__qualname__)}"
    }
    try:
        source = inspect.getsource(value).encode("utf-8")
    except (OSError, TypeError):
        code = getattr(value, "__code__", None)
        if code is None:
            return identity
        source = code.co_code + repr(code.co_consts).encode("utf-8")
    identity["implementation_sha256"] = _sha256(source)
    return identity


def component_identity(value: object | None) -> dict[str, Any] | None:
    """Describe model adapters without incorporating mutable counters or sessions."""
    if value is None:
        return None
    declared = getattr(value, "evaluation_identity", None)
    if callable(declared):
        result = declared()
        if not isinstance(result, Mapping):
            raise EvaluationIdentityError("evaluation_identity() must return a mapping")
        return dict(result)
    if inspect.isfunction(value) or inspect.ismethod(value):
        return _callable_code_identity(value)
    result: dict[str, Any] = {
        "kind": f"{type(value).__module__}.{type(value).__qualname__}",
    }
    if dataclasses.is_dataclass(value):
        configuration: dict[str, Any] = {}
        for field in dataclasses.fields(value):
            item = getattr(value, field.name)
            if isinstance(item, Path):
                configuration[field.name] = str(item.resolve())
            elif item is None or isinstance(item, (bool, float, int, str)):
                configuration[field.name] = item
        result["configuration"] = configuration
    try:
        result["implementation_sha256"] = _sha256(inspect.getsource(type(value)).encode("utf-8"))
    except (OSError, TypeError):
        pass
    return result


def build_manifest(
    repository: Path,
    *,
    cases: Iterable[object],
    seed_template: str,
    runner: object,
    judge: object | None,
    proposer: object | None,
    replicates: int,
    weights: Mapping[str, float],
) -> dict[str, Any]:
    """Bind all inputs that can change evaluation meaning or persisted GEPA state."""
    repository = repository.resolve()
    semantics = [
        repository / "eval" / name
        for name in ("cases.py", "gepa_harness.py", "identity.py", "runners.py", "scoring.py")
    ]
    semantics.extend(
        [
            repository / "src" / "oxide" / "planning.py",
            repository / "src" / "oxide" / "prompt_templates.py",
            repository / "src" / "oxide" / "roadmap.py",
            repository / "src" / "oxide" / "verification_policy.py",
            repository / "docs" / "VERIFICATION_PRIMER.md",
        ]
    )
    case_list = list(cases)
    payload: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "repository": str(repository),
        "seed_template_sha256": _sha256(seed_template.encode("utf-8")),
        "cases": [str(case.identifier) for case in case_list],
        "corpus_and_rubrics": _files(repository, corpus_files(case_list)),
        "scoring_implementation": _files(repository, semantics),
        "execution": {
            "runner": component_identity(runner),
            "judge": component_identity(judge),
            "proposer": component_identity(proposer),
            "replicates": replicates,
        },
        "weights": {key: float(value) for key, value in sorted(weights.items())},
    }
    payload["fingerprint"] = _sha256(_canonical(payload))
    return payload


def validate_manifest(manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict) or manifest.get("schema") != MANIFEST_SCHEMA:
        raise EvaluationIdentityError("evaluation manifest has an unsupported shape or schema")
    fingerprint = manifest.get("fingerprint")
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise EvaluationIdentityError("evaluation manifest fingerprint is invalid")
    unsigned = dict(manifest)
    unsigned.pop("fingerprint", None)
    if _sha256(_canonical(unsigned)) != fingerprint:
        raise EvaluationIdentityError("evaluation manifest fingerprint does not match its content")
    return manifest


def qualify_run_directory(run_directory: Path, expected: Mapping[str, Any]) -> Path:
    """Create or validate the immutable identity beside GEPA caches and state."""
    run_directory = run_directory.resolve()
    run_directory.mkdir(parents=True, exist_ok=True)
    validate_manifest(dict(expected))
    manifest_path = run_directory / MANIFEST_NAME
    if manifest_path.exists():
        try:
            current = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvaluationIdentityError(f"cannot read evaluation manifest: {error}") from error
        validate_manifest(current)
        if current != dict(expected):
            raise EvaluationIdentityError(
                "evaluation run identity differs from the existing manifest; use a new output directory"
            )
        return manifest_path
    existing = [path.name for path in run_directory.iterdir()]
    if existing:
        raise EvaluationIdentityError(
            "existing evaluation run has no identity manifest; use a new output directory"
        )
    payload = json.dumps(expected, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    try:
        descriptor = os.open(manifest_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return qualify_run_directory(run_directory, expected)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    return manifest_path
