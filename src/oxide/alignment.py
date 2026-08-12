"""Exact, fail-closed admission for generated implementation contracts."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from .roadmap import RoadmapError, validate_roadmap_approval
from .verification_policy import verification_policy_digest


class AlignmentError(RuntimeError):
    pass


_SCHEMA = "ContractAlignmentReceiptV1"
_ATTESTATION_SCHEMA = "OxideContractAttestationV3"
_APPROVAL_SCHEMA = "OxideContractApprovalV3"
_QUALIFICATION_SCHEMA = "OxideContractQualificationV3"
_SHA = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_GAP_FIELDS = (
    "ambiguities",
    "missing_acceptance_criteria",
    "unsupported_assumptions",
    "semantic_gaps",
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise AlignmentError(f"contract alignment receipt repeats field {key!r}")
        value[key] = item
    return value


def alignment_policy_digest() -> str:
    contract_module = Path(__file__).with_name("contract.py")
    closure = {
        "alignment": _digest(Path(__file__).read_bytes()),
        "contract": _digest(contract_module.read_bytes()),
        "roadmap": _digest(Path(__file__).with_name("roadmap.py").read_bytes()),
        "verification_policy_sha256": verification_policy_digest(),
        "schemas": [
            _SCHEMA,
            _ATTESTATION_SCHEMA,
            _APPROVAL_SCHEMA,
            _QUALIFICATION_SCHEMA,
        ],
    }
    return _digest(_canonical(closure))


def _git(
    repository: Path,
    *arguments: str,
    binary: bool = False,
    check: bool = True,
) -> bytes | str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=not binary,
        check=False,
    )
    if check and result.returncode:
        message = (result.stderr.decode(errors="replace") if binary else str(result.stderr)).strip()
        raise AlignmentError(message or "Git command failed during contract alignment")
    return result.stdout


def _blob(repository: Path, commit: str, path: str) -> bytes:
    value = _git(repository, "show", f"{commit}:{path}", binary=True)
    assert isinstance(value, bytes)
    return value


def _blob_id(repository: Path, commit: str, path: str) -> str:
    value = _git(repository, "rev-parse", f"{commit}:{path}")
    assert isinstance(value, str)
    return value.strip()


def _validate_ready_alignment(stage: dict[str, Any]) -> dict[str, Any]:
    alignment = stage.get("alignment")
    if not isinstance(alignment, dict):
        raise AlignmentError("contract has no alignment declaration")
    gaps = alignment.get("gaps")
    if not isinstance(gaps, dict) or set(gaps) != set(_GAP_FIELDS):
        raise AlignmentError("contract alignment gaps are malformed")
    unresolved = [f"{field}: {item}" for field in _GAP_FIELDS for item in gaps.get(field, [])]
    if alignment.get("contractible") is not True:
        raise AlignmentError("natural-language specification is not marked contractible")
    if unresolved:
        raise AlignmentError(
            "natural-language specification is not aligned: " + "; ".join(unresolved)
        )
    if alignment.get("proposed_revisions"):
        raise AlignmentError("approved alignment cannot retain proposed specification revisions")
    units = alignment.get("semantic_units")
    if (
        not isinstance(units, list)
        or not units
        or any(
            not isinstance(item, dict)
            or set(item) != {"id", "content", "sources"}
            or not isinstance(item.get("id"), str)
            or not item["id"]
            or not isinstance(item.get("content"), dict)
            or not item["content"]
            or not isinstance(item.get("sources"), list)
            or not item["sources"]
            for item in units
        )
        or len({str(item.get("id", "")) for item in units}) != len(units)
    ):
        raise AlignmentError("contract semantic trace is malformed")
    return alignment


def _specification_entries(
    target: Path,
    commit: str,
    stage: dict[str, Any],
) -> tuple[list[dict[str, str]], dict[str, str]]:
    alignment = _validate_ready_alignment(stage)
    entries: list[dict[str, str]] = []
    texts: dict[str, str] = {}
    for path in alignment["specifications"]:
        raw = _blob(target, commit, path)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise AlignmentError(f"approved specification is not UTF-8: {path}") from error
        entries.append(
            {
                "path": path,
                "blob": _blob_id(target, commit, path),
                "sha256": _digest(raw),
            }
        )
        texts[path] = text
    for unit in alignment["semantic_units"]:
        for source in unit["sources"]:
            if source["anchor"] not in texts[source["specification"]]:
                raise AlignmentError(
                    f"semantic unit {unit['id']} cites text absent from "
                    f"{source['specification']}: {source['anchor']!r}"
                )
    return entries, texts


def _generation_digest(
    source_commit: str,
    contract_sha256: str,
    specification_set_sha256: str,
    semantic_units_sha256: str,
) -> str:
    return _digest(
        _canonical(
            {
                "source_commit": source_commit,
                "contract_sha256": contract_sha256,
                "specification_set_sha256": specification_set_sha256,
                "semantic_units_sha256": semantic_units_sha256,
            }
        )
    )


def build_alignment_receipt(
    target: Path,
    contract_path: Path,
    stage: dict[str, Any],
    *,
    source_commit: str,
    agent_identity: str,
    user_identity: dict[str, str],
) -> dict[str, Any]:
    alignment = _validate_ready_alignment(stage)
    if _SHA.fullmatch(source_commit) is None:
        raise AlignmentError("alignment source commit is malformed")
    if (
        not isinstance(agent_identity, str)
        or not agent_identity.strip()
        or len(agent_identity.encode("utf-8")) > 256
        or any(character in agent_identity for character in "\x00\r\n")
    ):
        raise AlignmentError("contract-generation agent identity is malformed")
    if (
        not isinstance(user_identity, dict)
        or set(user_identity) != {"name", "email"}
        or not all(isinstance(value, str) and value.strip() for value in user_identity.values())
        or "@" not in user_identity["email"]
    ):
        raise AlignmentError("approving user Git identity is malformed")
    relative_contract = contract_path.resolve().relative_to(target.resolve()).as_posix()
    contract_raw = _blob(target, source_commit, relative_contract)
    specifications, _ = _specification_entries(target, source_commit, stage)
    semantic_units = alignment["semantic_units"]
    specification_set_sha256 = _digest(_canonical(specifications))
    semantic_units_sha256 = _digest(_canonical(semantic_units))
    contract_sha256 = _digest(contract_raw)
    return {
        "schema": _SCHEMA,
        "status": "aligned",
        "source_commit": source_commit,
        "contract_path": relative_contract,
        "contract_sha256": contract_sha256,
        "specifications": specifications,
        "specification_set_sha256": specification_set_sha256,
        "semantic_units": semantic_units,
        "semantic_units_sha256": semantic_units_sha256,
        "generation_sha256": _generation_digest(
            source_commit,
            contract_sha256,
            specification_set_sha256,
            semantic_units_sha256,
        ),
        "alignment_policy_sha256": alignment_policy_digest(),
        "agent": {
            "identity": agent_identity.strip(),
            "contractible": True,
            "faithful_to_approved_specification": True,
            "unresolved": {field: [] for field in _GAP_FIELDS},
        },
        "user": {
            "name": user_identity["name"].strip(),
            "email": user_identity["email"].strip(),
            "approved": True,
        },
    }


def write_alignment_receipt(
    target: Path,
    contract_path: Path,
    stage: dict[str, Any],
    *,
    source_commit: str,
    agent_identity: str,
    user_identity: dict[str, str],
) -> Path:
    receipt = build_alignment_receipt(
        target,
        contract_path,
        stage,
        source_commit=source_commit,
        agent_identity=agent_identity,
        user_identity=user_identity,
    )
    relative = stage["alignment"]["receipt"]
    destination = (target / relative).resolve()
    if not destination.is_relative_to(target.resolve()):
        raise AlignmentError("alignment receipt escaped the target repository")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination


def _validate_legacy_alignment_receipt(
    target: Path,
    base_commit: str,
    contract_path: Path,
    stage: dict[str, Any],
) -> dict[str, Any]:
    alignment = _validate_ready_alignment(stage)
    receipt_path = str(alignment["receipt"])
    try:
        raw = _blob(target, base_commit, receipt_path)
    except AlignmentError as error:
        raise AlignmentError(
            "current specification version has no committed contract-alignment approval receipt"
        ) from error
    try:
        receipt = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlignmentError("contract alignment receipt is unreadable") from error
    required = {
        "schema",
        "status",
        "source_commit",
        "contract_path",
        "contract_sha256",
        "specifications",
        "specification_set_sha256",
        "semantic_units",
        "semantic_units_sha256",
        "generation_sha256",
        "alignment_policy_sha256",
        "agent",
        "user",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise AlignmentError("contract alignment receipt has the wrong schema closure")
    if receipt["schema"] != _SCHEMA or receipt["status"] != "aligned":
        raise AlignmentError("contract alignment receipt is not an aligned receipt")
    source_commit = receipt["source_commit"]
    if not isinstance(source_commit, str) or _SHA.fullmatch(source_commit) is None:
        raise AlignmentError("contract alignment source commit is malformed")
    ancestor = subprocess.run(
        ["git", "-C", str(target), "merge-base", "--is-ancestor", source_commit, base_commit],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ancestor.returncode:
        raise AlignmentError("approved specification version is not an ancestor of the run base")
    relative_contract = contract_path.resolve().relative_to(target.resolve()).as_posix()
    if receipt["contract_path"] != relative_contract:
        raise AlignmentError("alignment receipt names a different generated contract")
    source_contract = _blob(target, source_commit, relative_contract)
    base_contract = _blob(target, base_commit, relative_contract)
    if source_contract != base_contract or receipt["contract_sha256"] != _digest(source_contract):
        raise AlignmentError(
            "generated contract changed after specification approval; regenerate and reapprove it"
        )
    specifications, _ = _specification_entries(target, source_commit, stage)
    for entry in specifications:
        if _blob_id(target, base_commit, entry["path"]) != entry["blob"]:
            raise AlignmentError(
                "approved natural-language specification changed; regenerate and reapprove the contract"
            )
    if receipt["specifications"] != specifications:
        raise AlignmentError("alignment receipt does not bind the approved specification closure")
    specification_set_sha256 = _digest(_canonical(specifications))
    if receipt["specification_set_sha256"] != specification_set_sha256:
        raise AlignmentError("alignment receipt specification digest is invalid")
    semantic_units = alignment["semantic_units"]
    semantic_units_sha256 = _digest(_canonical(semantic_units))
    if (
        receipt["semantic_units"] != semantic_units
        or receipt["semantic_units_sha256"] != semantic_units_sha256
    ):
        raise AlignmentError("generated contract semantics differ from the approved semantic trace")
    if receipt["alignment_policy_sha256"] != alignment_policy_digest():
        raise AlignmentError("contract-alignment policy changed; obtain a new approval")
    agent = receipt["agent"]
    if (
        not isinstance(agent, dict)
        or set(agent)
        != {"identity", "contractible", "faithful_to_approved_specification", "unresolved"}
        or not isinstance(agent["identity"], str)
        or not agent["identity"].strip()
        or agent["contractible"] is not True
        or agent["faithful_to_approved_specification"] is not True
        or agent["unresolved"] != {field: [] for field in _GAP_FIELDS}
    ):
        raise AlignmentError("contract-generation agent did not provide a complete attestation")
    user = receipt["user"]
    if (
        not isinstance(user, dict)
        or set(user) != {"name", "email", "approved"}
        or not isinstance(user["name"], str)
        or not user["name"].strip()
        or not isinstance(user["email"], str)
        or "@" not in user["email"]
        or user["approved"] is not True
    ):
        raise AlignmentError("current specification version lacks explicit user approval")
    generation_sha256 = _generation_digest(
        source_commit,
        receipt["contract_sha256"],
        specification_set_sha256,
        semantic_units_sha256,
    )
    if receipt["generation_sha256"] != generation_sha256:
        raise AlignmentError("alignment receipt does not bind this contract generation")
    if any(
        _DIGEST.fullmatch(str(receipt[field])) is None
        for field in (
            "contract_sha256",
            "specification_set_sha256",
            "semantic_units_sha256",
            "generation_sha256",
            "alignment_policy_sha256",
        )
    ):
        raise AlignmentError("contract alignment receipt contains a malformed digest")
    return {
        "schema": _SCHEMA,
        "receipt_path": receipt_path,
        "receipt_blob": _blob_id(target, base_commit, receipt_path),
        "receipt_sha256": _digest(raw),
        "source_commit": source_commit,
        "contract_sha256": receipt["contract_sha256"],
        "specification_set_sha256": specification_set_sha256,
        "semantic_units_sha256": semantic_units_sha256,
        "generation_sha256": generation_sha256,
        "alignment_policy_sha256": receipt["alignment_policy_sha256"],
        "agent_identity": agent["identity"],
        "approved_by": {"name": user["name"], "email": user["email"]},
    }


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _v2_alignment(stage: dict[str, Any]) -> dict[str, Any]:
    alignment = _validate_ready_alignment(stage)
    required = {
        "specifications",
        "contractible",
        "gaps",
        "proposed_revisions",
        "semantic_units",
        "roadmap",
        "roadmap_stage",
        "roadmap_approval",
        "attestation",
        "approval",
        "qualification",
        "implementation_goals",
        "verification_goals",
    }
    if set(alignment) != required:
        raise AlignmentError("interactive contract alignment declaration is malformed")
    return alignment


def _v2_binding_summary(binding: dict[str, Any]) -> dict[str, Any]:
    return {
        "roadmap_path": binding["roadmap_path"],
        "stage_id": binding["stage_id"],
        "stage_sha256": binding["stage_sha256"],
        "global_invariants_sha256": binding["global_invariants_sha256"],
        "semantic_closure": binding["semantic_closure"],
        "semantic_closure_sha256": binding["semantic_closure_sha256"],
        "verification_policy_sha256": verification_policy_digest(),
    }


def validate_interactive_trace(stage: dict[str, Any], binding: dict[str, Any]) -> None:
    alignment = _v2_alignment(stage)
    if (
        stage.get("verification", {}).get("verification_policy_sha256")
        != verification_policy_digest()
    ):
        raise AlignmentError("contract names a stale Oxide verification policy")
    allowed = {
        (item["path"], item["anchor"], item["requirement"]) for item in binding["semantic_closure"]
    }
    cited: set[tuple[str, str, str]] = set()
    for unit in alignment["semantic_units"]:
        for source in unit["sources"]:
            identity = (
                str(source.get("specification", "")),
                str(source.get("anchor", "")),
                str(source.get("requirement", "")),
            )
            if identity not in allowed:
                raise AlignmentError(
                    f"generated semantic unit {unit['id']} is absent from the approved "
                    "roadmap-stage closure"
                )
            cited.add(identity)
    expected_specifications = sorted({path for path, _, _ in allowed})
    if sorted(alignment["specifications"]) != expected_specifications:
        raise AlignmentError("contract specification list differs from the selected-stage closure")
    if not cited:
        raise AlignmentError("generated contract has no exact source trace")
    roadmap_stage = binding["stage"]
    if stage["goal"] != roadmap_stage["outcome"]:
        raise AlignmentError("contract goal differs from the approved roadmap-stage outcome")
    if alignment["implementation_goals"] != roadmap_stage["implementation_goals"]:
        raise AlignmentError("contract implementation goals differ from the approved roadmap stage")
    if alignment["verification_goals"] != roadmap_stage["verification_goals"]:
        raise AlignmentError("contract verification goals differ from the approved roadmap stage")


def _v2_generation(
    contract_raw: bytes,
    stage: dict[str, Any],
    binding: dict[str, Any],
) -> dict[str, str]:
    contract_sha256 = _digest(contract_raw)
    semantic_units_sha256 = _digest(_canonical(stage["alignment"]["semantic_units"]))
    stage_binding_sha256 = _digest(_canonical(_v2_binding_summary(binding)))
    policy_sha256 = alignment_policy_digest()
    generation_sha256 = _digest(
        _canonical(
            {
                "contract_sha256": contract_sha256,
                "semantic_units_sha256": semantic_units_sha256,
                "stage_binding_sha256": stage_binding_sha256,
                "alignment_policy_sha256": policy_sha256,
                "verification_policy_sha256": verification_policy_digest(),
            }
        )
    )
    return {
        "contract_sha256": contract_sha256,
        "semantic_units_sha256": semantic_units_sha256,
        "stage_binding_sha256": stage_binding_sha256,
        "alignment_policy_sha256": policy_sha256,
        "verification_policy_sha256": verification_policy_digest(),
        "generation_sha256": generation_sha256,
    }


def write_interactive_alignment_receipts(
    target: Path,
    contract_path: Path,
    stage: dict[str, Any],
    *,
    agent_identity: str,
    user_identity: dict[str, str],
) -> list[Path]:
    """Write separate agent, user, and mechanical receipts after interactive approval."""
    alignment = _v2_alignment(stage)
    if not isinstance(agent_identity, str) or not agent_identity.strip():
        raise AlignmentError("contract-generation agent identity is malformed")
    if (
        not isinstance(user_identity, dict)
        or set(user_identity) != {"name", "email"}
        or not all(isinstance(value, str) and value.strip() for value in user_identity.values())
        or "@" not in user_identity["email"]
    ):
        raise AlignmentError("approving user Git identity is malformed")
    try:
        roadmap_approval = validate_roadmap_approval(
            target,
            alignment["roadmap"],
            alignment["roadmap_stage"],
            receipt_path=alignment["roadmap_approval"],
        )
        binding = roadmap_approval["binding"]
    except RoadmapError as error:
        raise AlignmentError(str(error)) from error
    validate_interactive_trace(stage, binding)
    contract_raw = contract_path.read_bytes()
    generation = _v2_generation(contract_raw, stage, binding)
    repository_revision = str(_git(target, "rev-parse", "HEAD")).strip()
    summary = _v2_binding_summary(binding)
    agent = {
        "schema": _ATTESTATION_SCHEMA,
        "status": "attested",
        "repository_revision": repository_revision,
        "contract_path": contract_path.resolve().relative_to(target.resolve()).as_posix(),
        **generation,
        "stage_binding": summary,
        "agent": {
            "identity": agent_identity.strip(),
            "contractible": True,
            "faithful_to_approved_sources": True,
            "introduces_no_product_semantics": True,
            "unresolved": {field: [] for field in _GAP_FIELDS},
        },
    }
    user = {
        "schema": _APPROVAL_SCHEMA,
        "status": "approved",
        "repository_revision": repository_revision,
        "contract_path": agent["contract_path"],
        **generation,
        "stage_binding": summary,
        "verification_goals": alignment["verification_goals"],
        "user": {
            "name": user_identity["name"].strip(),
            "email": user_identity["email"].strip(),
            "approved_stage_meaning": True,
            "approved_contract_and_verification_goals": True,
        },
    }
    qualification = {
        "schema": _QUALIFICATION_SCHEMA,
        "status": "qualified",
        "repository_revision": repository_revision,
        "contract_path": agent["contract_path"],
        **generation,
        "stage_binding": summary,
        "checks": {
            "contract_schema": True,
            "roadmap_stage_approved": True,
            "semantic_trace_closed": True,
            "source_requirements_present": True,
            "unresolved_gaps_absent": True,
            "verification_policy_current": True,
        },
    }
    outputs = [
        (alignment["attestation"], agent),
        (alignment["approval"], user),
        (alignment["qualification"], qualification),
    ]
    paths: list[Path] = []
    for relative, value in outputs:
        path = (target / relative).resolve()
        if not path.is_relative_to(target.resolve()):
            raise AlignmentError("contract approval artifact escaped the target repository")
        _atomic_json(path, value)
        paths.append(path)
    return paths


def _strict_receipt(raw: bytes, schema: str, fields: set[str], description: str) -> dict[str, Any]:
    try:
        receipt = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise AlignmentError(f"{description} is unreadable") from error
    if not isinstance(receipt, dict) or set(receipt) != fields or receipt.get("schema") != schema:
        raise AlignmentError(f"{description} has the wrong schema closure")
    return receipt


def _validate_interactive_alignment_receipts(
    target: Path,
    base_commit: str,
    contract_path: Path,
    stage: dict[str, Any],
) -> dict[str, Any]:
    alignment = _v2_alignment(stage)
    try:
        roadmap_approval = validate_roadmap_approval(
            target,
            alignment["roadmap"],
            alignment["roadmap_stage"],
            commit=base_commit,
            receipt_path=alignment["roadmap_approval"],
        )
    except RoadmapError as error:
        raise AlignmentError(str(error)) from error
    binding = roadmap_approval["binding"]
    validate_interactive_trace(stage, binding)
    relative_contract = contract_path.resolve().relative_to(target.resolve()).as_posix()
    contract_raw = _blob(target, base_commit, relative_contract)
    generation = _v2_generation(contract_raw, stage, binding)
    common = {
        "schema",
        "status",
        "repository_revision",
        "contract_path",
        *generation,
        "stage_binding",
    }
    paths = {
        "attestation": alignment["attestation"],
        "approval": alignment["approval"],
        "qualification": alignment["qualification"],
    }
    raw_receipts = {name: _blob(target, base_commit, path) for name, path in paths.items()}
    attestation = _strict_receipt(
        raw_receipts["attestation"],
        _ATTESTATION_SCHEMA,
        common | {"agent"},
        "contract agent attestation",
    )
    approval = _strict_receipt(
        raw_receipts["approval"],
        _APPROVAL_SCHEMA,
        common | {"verification_goals", "user"},
        "contract user approval",
    )
    qualification = _strict_receipt(
        raw_receipts["qualification"],
        _QUALIFICATION_SCHEMA,
        common | {"checks"},
        "contract qualification receipt",
    )
    expected_summary = _v2_binding_summary(binding)
    for name, receipt in (
        ("attestation", attestation),
        ("approval", approval),
        ("qualification", qualification),
    ):
        if receipt.get("contract_path") != relative_contract:
            raise AlignmentError(f"{name} receipt names a different contract")
        if receipt.get("stage_binding") != expected_summary:
            raise AlignmentError(f"{name} receipt names a stale roadmap-stage closure")
        if any(receipt.get(field) != value for field, value in generation.items()):
            raise AlignmentError(f"{name} receipt does not bind the current contract generation")
    agent = attestation.get("agent")
    if (
        attestation.get("status") != "attested"
        or not isinstance(agent, dict)
        or set(agent)
        != {
            "identity",
            "contractible",
            "faithful_to_approved_sources",
            "introduces_no_product_semantics",
            "unresolved",
        }
        or not isinstance(agent["identity"], str)
        or not agent["identity"].strip()
        or agent["contractible"] is not True
        or agent["faithful_to_approved_sources"] is not True
        or agent["introduces_no_product_semantics"] is not True
        or agent["unresolved"] != {field: [] for field in _GAP_FIELDS}
    ):
        raise AlignmentError("contract agent attestation is incomplete")
    user = approval.get("user")
    if (
        approval.get("status") != "approved"
        or not isinstance(user, dict)
        or set(user)
        != {
            "name",
            "email",
            "approved_stage_meaning",
            "approved_contract_and_verification_goals",
        }
        or not isinstance(user["name"], str)
        or not user["name"].strip()
        or not isinstance(user["email"], str)
        or "@" not in user["email"]
        or user["approved_stage_meaning"] is not True
        or user["approved_contract_and_verification_goals"] is not True
        or approval.get("verification_goals") != alignment["verification_goals"]
    ):
        raise AlignmentError("current stage generation lacks explicit user approval")
    expected_checks = {
        "contract_schema": True,
        "roadmap_stage_approved": True,
        "semantic_trace_closed": True,
        "source_requirements_present": True,
        "unresolved_gaps_absent": True,
        "verification_policy_current": True,
    }
    if qualification.get("status") != "qualified" or qualification.get("checks") != expected_checks:
        raise AlignmentError("contract generation was not mechanically qualified")
    return {
        "schema": "OxideInteractiveAlignmentBindingV3",
        "receipt_paths": [alignment["roadmap_approval"], *paths.values()],
        "receipts": {
            name: {
                "path": paths[name],
                "blob": _blob_id(target, base_commit, paths[name]),
                "sha256": _digest(raw_receipts[name]),
            }
            for name in paths
        },
        "roadmap_approval": {
            key: roadmap_approval[key]
            for key in (
                "receipt_path",
                "receipt_sha256",
                "stage_id",
                "stage_sha256",
                "global_invariants_sha256",
                "semantic_closure_sha256",
                "planning_policy_sha256",
                "verification_policy_sha256",
            )
        },
        **generation,
        "agent_identity": agent["identity"],
        "approved_by": {"name": user["name"], "email": user["email"]},
    }


def validate_alignment_receipt(
    target: Path,
    base_commit: str,
    contract_path: Path,
    stage: dict[str, Any],
) -> dict[str, Any]:
    """Validate legacy schema-3 or interactive schema-4 admission artifacts."""
    if stage.get("contract", {}).get("schema") == 4:
        return _validate_interactive_alignment_receipts(target, base_commit, contract_path, stage)
    return _validate_legacy_alignment_receipt(target, base_commit, contract_path, stage)
