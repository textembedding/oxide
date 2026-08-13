"""Load a target-owned formal Rust implementation contract into the generic workflow DAG."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any

from .roadmap import canonical_source_anchor, canonical_source_text
from .verification_policy import verification_policy_digest


class ContractError(RuntimeError):
    pass


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_VERUS_OPERATIONS = {"proof", "gate", "composition"}
_ALIGNMENT_GAPS = (
    "ambiguities",
    "missing_acceptance_criteria",
    "unsupported_assumptions",
    "semantic_gaps",
)


def contract_payload_digest(value: dict[str, Any]) -> str:
    """Bind contract semantics without self-referential decision metadata."""
    payload = dict(value)
    payload.pop("attestation", None)
    payload.pop("approval", None)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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


def _source_refs(
    value: object,
    field: str,
    specifications: set[str],
    *,
    exact_requirements: bool = False,
) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{field} must be a nonempty list of specification citations")
    normalized: list[dict[str, str]] = []
    identities: set[tuple[str, ...]] = set()
    expected = (
        {"specification", "anchor", "requirement"}
        if exact_requirements
        else {
            "specification",
            "anchor",
        }
    )
    for ordinal, raw in enumerate(value, 1):
        if not isinstance(raw, dict) or set(raw) != expected:
            fields = ", ".join(sorted(expected))
            raise ContractError(f"{field}[{ordinal}] must contain exactly {fields}")
        specification = _relative_path(raw.get("specification"), f"{field}[{ordinal}]")
        anchor = raw.get("anchor")
        if specification not in specifications:
            raise ContractError(f"{field}[{ordinal}] cites an undeclared specification")
        if (
            not isinstance(anchor, str)
            or not anchor.strip()
            or len(anchor.encode("utf-8")) > 512
            or any(character in anchor for character in "\x00\r")
        ):
            raise ContractError(f"{field}[{ordinal}].anchor is malformed")
        requirement = raw.get("requirement")
        if exact_requirements and (
            not isinstance(requirement, str)
            or not requirement.strip()
            or len(requirement.encode("utf-8")) > 8192
            or "\x00" in requirement
        ):
            raise ContractError(f"{field}[{ordinal}].requirement is malformed")
        normalized_anchor = canonical_source_anchor(anchor)
        identity = (
            (specification, normalized_anchor, canonical_source_text(requirement))
            if exact_requirements
            else (specification, normalized_anchor)
        )
        if identity in identities:
            raise ContractError(f"{field} contains duplicate citations")
        identities.add(identity)
        citation = {"specification": specification, "anchor": normalized_anchor}
        if exact_requirements:
            citation["requirement"] = canonical_source_text(requirement)
        normalized.append(citation)
    return normalized


def _check(
    value: object,
    ordinal: int,
    task_id: str,
    specifications: set[str],
    *,
    exact_requirements: bool = False,
) -> dict[str, Any]:
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
        "sources",
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
        "sources": _source_refs(
            check.get("sources"),
            f"task {task_id} check {check_id}.sources",
            specifications,
            exact_requirements=exact_requirements,
        ),
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
    value: object,
    source: str | Path = "verification/contract.toml",
    *,
    require_approval: bool = True,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ContractError("verification contract must be a TOML table")
    contract = dict(value)
    schema = contract.get("schema")
    if schema not in {3, 5}:
        raise ContractError("unsupported verification contract schema")
    identifier = contract.get("id")
    if not isinstance(identifier, str) or not _SAFE_ID.fullmatch(identifier):
        raise ContractError("contract.id must be a safe run identifier")
    if contract.get("enabled") is not True:
        raise ContractError("verification contract is disabled")
    goal = contract.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise ContractError("contract.goal must be nonempty")
    policy_sha256 = contract.get("verification_policy_sha256")
    if policy_sha256 != verification_policy_digest():
        raise ContractError(
            "contract.verification_policy_sha256 must bind the current Oxide verification policy"
        )
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
    alignment = contract.get("alignment")
    if not isinstance(alignment, dict):
        raise ContractError("contract.alignment must be a table")
    allowed_alignment = {
        "specifications",
        "contractible",
        "goal_sources",
        "proposed_revisions",
        *_ALIGNMENT_GAPS,
    }
    if schema == 3:
        allowed_alignment.add("receipt")
    else:
        allowed_alignment.update(
            {
                "roadmap",
                "roadmap_stages",
                "implementation_goals",
                "verification_goals",
            }
        )
    unexpected_alignment = set(alignment) - allowed_alignment
    if unexpected_alignment:
        raise ContractError(
            f"contract.alignment has unsupported fields: {sorted(unexpected_alignment)}"
        )
    specifications = [
        _relative_path(item, "contract.alignment.specifications")
        for item in _string_list(
            alignment.get("specifications"),
            "contract.alignment.specifications",
            nonempty=True,
        )
    ]
    if any(Path(path).suffix.lower() != ".md" for path in specifications):
        raise ContractError("contract.alignment specifications must be Markdown files")
    specification_set = set(specifications)
    receipt_paths: list[str]
    if schema == 3:
        receipt = _relative_path(alignment.get("receipt"), "contract.alignment.receipt")
        if not receipt.startswith("verification/") or not receipt.endswith(".json"):
            raise ContractError(
                "contract.alignment.receipt must be a JSON file under verification/"
            )
        receipt_paths = [receipt]
        roadmap = None
        implementation_goals: list[str] = []
        verification_goals: list[str] = []
    else:
        roadmap = _relative_path(alignment.get("roadmap"), "contract.alignment.roadmap")
        if roadmap != "ROADMAP.md":
            raise ContractError("schema 5 contract.alignment.roadmap must be ROADMAP.md")
        roadmap_stages = _string_list(
            alignment.get("roadmap_stages"),
            "contract.alignment.roadmap_stages",
            nonempty=True,
        )
        if any(_SAFE_ID.fullmatch(item) is None for item in roadmap_stages):
            raise ContractError("contract.alignment.roadmap_stages contains an invalid phase ID")
        declared_stages = _string_list(contract.get("stages"), "contract.stages", nonempty=True)
        if declared_stages != roadmap_stages:
            raise ContractError("contract.stages must equal contract.alignment.roadmap_stages")
        receipt_paths = []
        implementation_goals = _string_list(
            alignment.get("implementation_goals"),
            "contract.alignment.implementation_goals",
            nonempty=True,
        )
        verification_goals = _string_list(
            alignment.get("verification_goals"),
            "contract.alignment.verification_goals",
            nonempty=True,
        )
    contractible = alignment.get("contractible")
    if not isinstance(contractible, bool):
        raise ContractError("contract.alignment.contractible must be boolean")
    gaps = {
        field: _string_list(alignment.get(field, []), f"contract.alignment.{field}")
        for field in _ALIGNMENT_GAPS
    }
    proposed_revisions = _string_list(
        alignment.get("proposed_revisions", []),
        "contract.alignment.proposed_revisions",
    )
    if schema == 5:
        decision_fields = {"binding", "attestation", "approval"}
        if not require_approval and (decision_fields & set(contract)):
            raise ContractError(
                "draft schema 5 contracts must leave binding and approval metadata to Oxide"
            )
        if require_approval and not decision_fields <= set(contract):
            raise ContractError(
                "schema 5 contract lacks its semantic binding, attestation, or user approval"
            )
        binding = contract.get("binding")
        if require_approval and (
            not isinstance(binding, dict)
            or set(binding)
            != {
                "stage_set_sha256",
                "global_invariants_sha256",
                "semantic_closure_sha256",
            }
            or any(_DIGEST.fullmatch(str(value)) is None for value in binding.values())
        ):
            raise ContractError("contract semantic phase-set binding is malformed")
        payload_sha256 = contract_payload_digest(contract)
        attestation = contract.get("attestation")
        approval = contract.get("approval")
        if require_approval and (
            not isinstance(attestation, dict)
            or set(attestation)
            != {
                "identity",
                "payload_sha256",
                "contractible",
                "faithful_to_approved_sources",
                "introduces_no_product_semantics",
                "unresolved",
            }
            or not isinstance(attestation.get("identity"), str)
            or not attestation["identity"].strip()
            or attestation.get("payload_sha256") != payload_sha256
            or attestation.get("contractible") is not True
            or attestation.get("faithful_to_approved_sources") is not True
            or attestation.get("introduces_no_product_semantics") is not True
            or attestation.get("unresolved") != []
        ):
            raise ContractError("contract agent attestation is incomplete or stale")
        if require_approval and (
            not isinstance(approval, dict)
            or set(approval) != {"user_name", "user_email", "payload_sha256", "approved"}
            or not isinstance(approval.get("user_name"), str)
            or not approval["user_name"].strip()
            or not isinstance(approval.get("user_email"), str)
            or "@" not in approval["user_email"]
            or approval.get("payload_sha256") != payload_sha256
            or approval.get("approved") is not True
        ):
            raise ContractError("contract lacks current explicit user approval")
    if any(gaps.values()) and not proposed_revisions:
        raise ContractError("unresolved alignment gaps require concrete proposed revisions")
    goal_sources = _source_refs(
        alignment.get("goal_sources"),
        "contract.alignment.goal_sources",
        specification_set,
        exact_requirements=schema == 5,
    )

    def covered(path: str) -> bool:
        return any(
            path == configured.rstrip("/") or path.startswith(configured.rstrip("/") + "/")
            for configured in immutable_paths
        )

    closure_paths = [*specifications, *receipt_paths]
    if roadmap is not None:
        closure_paths.append(roadmap)
    for path in closure_paths:
        if not covered(path):
            raise ContractError(f"contract.immutable_paths must freeze alignment input {path}")
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
        allowed_task_fields = {"id", "title", "prompt", "depends_on", "sources", "checks"}
        if schema == 5:
            allowed_task_fields.add("phase")
        if set(task) != allowed_task_fields:
            raise ContractError(f"task {task_id} has unsupported or missing fields")
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
        phase = task.get("phase") if schema == 5 else None
        if schema == 5 and phase not in roadmap_stages:
            raise ContractError(f"task {task_id} names an unselected roadmap phase")
        task_sources = _source_refs(
            task.get("sources"),
            f"task {task_id}.sources",
            specification_set,
            exact_requirements=schema == 5,
        )
        checks = [
            _check(
                item,
                index,
                task_id,
                specification_set,
                exact_requirements=schema == 5,
            )
            for index, item in enumerate(raw_checks, 1)
        ]
        if len({item["id"] for item in checks}) != len(checks):
            raise ContractError(f"task {task_id} has duplicate check ids")
        normalized_task = {
            "id": task_id,
            "title": title,
            "prompt": prompt,
            "depends_on": list(dependencies),
            "checks": checks,
            "sources": task_sources,
        }
        if schema == 5:
            normalized_task["phase"] = phase
        normalized_tasks.append(normalized_task)
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
    selected_stage = contract.get("stage", identifier)
    if schema == 5 and "stage" in contract:
        raise ContractError("schema 5 contracts use contract.stages, not contract.stage")
    normalized_alignment: dict[str, Any] = {
        "specifications": specifications,
        "contractible": contractible,
        "gaps": gaps,
        "proposed_revisions": proposed_revisions,
        "semantic_units": [
            {
                "id": "goal",
                "content": {"goal": goal},
                "sources": goal_sources,
            },
            *[
                {
                    "id": f"task:{task['id']}",
                    "content": {
                        "title": task["title"],
                        "prompt": task["prompt"],
                        "depends_on": task["depends_on"],
                        **({"phase": task["phase"]} if schema == 5 else {}),
                    },
                    "sources": task["sources"],
                }
                for task in normalized_tasks
            ],
            *[
                {
                    "id": f"check:{task['id']}:{check['id']}",
                    "content": {
                        key: check[key]
                        for key in (
                            "driver",
                            "command",
                            "operation",
                            "root",
                            "working_directory",
                            "environment",
                            "artifacts",
                            "evidence_slot",
                            "receipt_required",
                        )
                        if key in check
                    },
                    "sources": check["sources"],
                }
                for task in normalized_tasks
                for check in task["checks"]
            ],
        ],
    }
    if schema == 3:
        normalized_alignment["receipt"] = receipt_paths[0]
    else:
        normalized_alignment.update(
            {
                "roadmap": roadmap,
                "roadmap_stages": roadmap_stages,
                "implementation_goals": implementation_goals,
                "verification_goals": verification_goals,
            }
        )
    stage = {
        "stage": selected_stage if schema != 5 else "+".join(roadmap_stages),
        "stages": roadmap_stages if schema == 5 else [selected_stage],
        "enabled": True,
        "goal": goal,
        "minimum_reviews": minimum_reviews,
        "tasks": normalized_tasks,
        "verification": {
            "immutable_paths": immutable_paths,
            "qualification": ["toolchain", "policy"],
            "candidate_operation": "policy",
            "prospective_operation": "gate",
            "prospective_receipt_required": True,
            "evidence_policy": evidence_policy,
            "timeout_seconds": timeout,
            "infrastructure_exit_codes": list(infrastructure),
            "max_artifact_bytes": maximum,
            "verification_policy_sha256": policy_sha256,
        },
        "alignment": normalized_alignment,
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
