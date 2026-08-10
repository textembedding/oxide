"""Load a target-owned formal Rust implementation contract into the generic workflow DAG."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any


class ContractError(RuntimeError):
    pass


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_VERUS_OPERATIONS = {"proof", "gate", "composition"}


def _relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ContractError(f"{field} must be a nonempty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ContractError(f"{field} must not escape the target repository")
    return path.as_posix()


def _string_list(value: object, field: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ContractError(f"{field} must be a list of nonempty strings")
    if nonempty and not value:
        raise ContractError(f"{field} must not be empty")
    if len(set(value)) != len(value):
        raise ContractError(f"{field} contains duplicates")
    return list(value)


def _check(value: object, ordinal: int, task_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"task {task_id} check {ordinal} must be a table")
    check = dict(value)
    check_id = check.get("id", f"check-{ordinal}")
    driver = check.get("driver")
    if not isinstance(check_id, str) or not _SAFE_ID.fullmatch(check_id):
        raise ContractError(f"task {task_id} check {ordinal} has an invalid id")
    if driver not in {"command", "verus"}:
        raise ContractError(f"task {task_id} check {check_id} has an invalid driver")
    common = {
        "id",
        "driver",
        "working_directory",
        "environment",
        "artifacts",
        "evidence_slot",
    }
    allowed = common | (
        {"command", "receipt_required"} if driver == "command" else {"operation", "root"}
    )
    unexpected = set(check) - allowed
    if unexpected:
        raise ContractError(
            f"task {task_id} check {check_id} has unsupported fields: {sorted(unexpected)}"
        )
    working_directory = _relative_path(
        check.get("working_directory", "."),
        f"task {task_id} check {check_id}.working_directory",
    )
    environment = check.get("environment", {})
    artifacts = check.get("artifacts", [])
    evidence_slot = check.get("evidence_slot", "qualified-once")
    if (
        not isinstance(environment, dict)
        or any(
            not isinstance(key, str) or not isinstance(item, str)
            for key, item in environment.items()
        )
        or not isinstance(artifacts, list)
        or any(not isinstance(item, str) or not item for item in artifacts)
        or not isinstance(evidence_slot, str)
        or _SAFE_ID.fullmatch(evidence_slot) is None
    ):
        raise ContractError(f"task {task_id} check {check_id} has malformed execution inputs")
    normalized: dict[str, Any] = {
        "id": check_id,
        "driver": driver,
        "working_directory": working_directory,
        "environment": dict(sorted(environment.items())),
        "artifacts": [
            _relative_path(item, f"task {task_id} check {check_id}.artifacts") for item in artifacts
        ],
        "evidence_slot": evidence_slot,
    }
    if driver == "command":
        command = check.get("command")
        receipt_required = check.get("receipt_required", False)
        if not isinstance(command, str) or not command.strip():
            raise ContractError(f"task {task_id} check {check_id}.command must be nonempty")
        if not isinstance(receipt_required, bool):
            raise ContractError(f"task {task_id} check {check_id}.receipt_required must be boolean")
        normalized.update(command=command, receipt_required=receipt_required)
        return normalized
    operation = check.get("operation")
    root = check.get("root")
    if operation not in _VERUS_OPERATIONS:
        raise ContractError(f"task {task_id} check {check_id} has an invalid Verus operation")
    if operation == "proof":
        root = _relative_path(root, f"task {task_id} check {check_id}.root")
    elif root is not None:
        raise ContractError(f"task {task_id} check {check_id} must not set root for {operation}")
    display = f"oxide-verus {operation}" + (f" --root {root}" if root else "")
    normalized.update(
        command=display,
        operation=operation,
        root=root,
        receipt_required=operation in {"gate", "composition"},
    )
    return normalized


def validate_contract(
    value: object, source: str | Path = "verification/contract.toml"
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("verification contract must be a TOML table")
    contract = dict(value)
    if contract.get("schema") != 2:
        raise ContractError("unsupported verification contract schema")
    identifier = contract.get("id")
    if not isinstance(identifier, str) or not _SAFE_ID.fullmatch(identifier):
        raise ContractError("contract.id must be a safe run identifier")
    if contract.get("enabled") is not True:
        raise ContractError("verification contract is disabled")
    goal = contract.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ContractError("contract.goal must be nonempty")
    minimum_reviews = contract.get("minimum_reviews", 3)
    if (
        isinstance(minimum_reviews, bool)
        or not isinstance(minimum_reviews, int)
        or not 1 <= minimum_reviews <= 16
    ):
        raise ContractError("contract.minimum_reviews must be between 1 and 16")
    immutable_paths = [
        _relative_path(item, "contract.immutable_paths")
        for item in _string_list(
            contract.get("immutable_paths"), "contract.immutable_paths", nonempty=True
        )
    ]
    execution = contract.get("execution")
    if not isinstance(execution, dict):
        raise ContractError("contract.execution must be a table")
    evidence_policy = execution.get("evidence_policy", "exact-verus-context-v1")
    timeout = execution.get("timeout_seconds", 1800)
    maximum = execution.get("max_artifact_bytes", 16777216)
    infrastructure = execution.get("infrastructure_exit_codes", [2, 124])
    if (
        not isinstance(evidence_policy, str)
        or _SAFE_ID.fullmatch(evidence_policy) is None
        or isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 1 <= timeout <= 86400
        or isinstance(maximum, bool)
        or not isinstance(maximum, int)
        or not 1 <= maximum <= 67108864
        or not isinstance(infrastructure, list)
        or not infrastructure
        or any(
            isinstance(item, bool) or not isinstance(item, int) or not 2 <= item <= 255
            for item in infrastructure
        )
        or len(set(infrastructure)) != len(infrastructure)
    ):
        raise ContractError("contract.execution policy is malformed")
    tasks = contract.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ContractError("verification contract must contain tasks")
    identifiers: set[str] = set()
    normalized_tasks: list[dict[str, Any]] = []
    for task in tasks:
        if not isinstance(task, dict):
            raise ContractError("contract tasks must be tables")
        task_id = task.get("id")
        if (
            not isinstance(task_id, str)
            or not _SAFE_ID.fullmatch(task_id)
            or task_id in identifiers
        ):
            raise ContractError(f"task id is duplicate or invalid: {task_id!r}")
        identifiers.add(task_id)
        title, prompt = task.get("title"), task.get("prompt")
        dependencies = task.get("depends_on", [])
        raw_checks = task.get("checks")
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(prompt, str)
            or not prompt.strip()
            or not isinstance(dependencies, list)
            or any(not isinstance(item, str) or not item for item in dependencies)
            or len(set(dependencies)) != len(dependencies)
            or not isinstance(raw_checks, list)
            or not raw_checks
        ):
            raise ContractError(f"task {task_id} is incomplete")
        checks = [_check(item, index, task_id) for index, item in enumerate(raw_checks, 1)]
        if len({item["id"] for item in checks}) != len(checks):
            raise ContractError(f"task {task_id} has duplicate check ids")
        normalized_tasks.append(
            {
                "id": task_id,
                "title": title,
                "prompt": prompt,
                "depends_on": list(dependencies),
                "checks": checks,
            }
        )
    dependencies = {task["id"]: set(task["depends_on"]) for task in normalized_tasks}
    for task_id, required in dependencies.items():
        if task_id in required or not required <= identifiers:
            raise ContractError(f"task {task_id} has invalid dependencies")
    remaining = set(dependencies)
    while remaining:
        ready = {task_id for task_id in remaining if not dependencies[task_id] & remaining}
        if not ready:
            raise ContractError("verification contract task graph contains a cycle")
        remaining -= ready
    stage = {
        "stage": contract.get("stage", identifier),
        "enabled": True,
        "goal": goal,
        "minimum_reviews": minimum_reviews,
        "tasks": normalized_tasks,
        "verification": {
            "immutable_paths": immutable_paths,
            "qualification": ["toolchain", "policy"],
            "prospective_operation": "gate",
            "prospective_receipt_required": True,
            "evidence_policy": evidence_policy,
            "timeout_seconds": timeout,
            "infrastructure_exit_codes": list(infrastructure),
            "max_artifact_bytes": maximum,
        },
        "contract": contract,
    }
    try:
        json.dumps(stage, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise ContractError(f"contract contains a non-canonical value: {source}") from error
    return stage


def load_contract(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        with source.open("rb") as handle:
            value = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ContractError(f"invalid verification contract {source}: {error}") from error
    return validate_contract(value, source)
