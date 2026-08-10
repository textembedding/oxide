"""Build authoritative invocations of the harness-owned Verus engine."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any

from .engine import engine_digest

ENGINE_PATH = Path(__file__).with_name("engine.py").resolve()


def invocation(
    repository: str | Path,
    contract_root: str | Path,
    operation: str,
    *,
    contract_path: str = "verification/contract.toml",
    root: str | None = None,
    candidate_tree: str | None = None,
    prospective_tree: str | None = None,
    receipt: str | Path | None = None,
    artifact_dir: str | Path | None = None,
) -> list[str]:
    if operation not in {"toolchain", "policy", "proof", "gate", "composition"}:
        raise ValueError(f"unsupported Verus operation: {operation}")
    command = [
        sys.executable,
        str(ENGINE_PATH),
        "--repository",
        str(Path(repository).resolve()),
        "--contract-root",
        str(Path(contract_root).resolve()),
        "--contract",
        contract_path,
        operation,
    ]
    if operation == "proof":
        if not root:
            raise ValueError("proof operation requires a root")
        command.extend(["--root", root])
        if prospective_tree:
            command.extend(["--prospective-tree", prospective_tree])
    elif operation in {"gate", "composition"}:
        if candidate_tree:
            command.extend(["--candidate-tree", candidate_tree])
        if prospective_tree:
            command.extend(["--prospective-tree", prospective_tree])
        if receipt:
            command.extend(["--receipt", str(receipt)])
        if artifact_dir:
            command.extend(["--artifact-dir", str(artifact_dir)])
    return command


def display(check: dict[str, Any]) -> str:
    if check.get("driver") == "verus":
        parts = ["oxide-verus", str(check.get("operation", ""))]
        if check.get("root"):
            parts.extend(["--root", str(check["root"])])
        return shlex.join(parts)
    return str(check.get("command", ""))


__all__ = ["ENGINE_PATH", "display", "engine_digest", "invocation"]
