"""Standardized, source-traced roadmaps and phase-set bindings."""

from __future__ import annotations

import hashlib
import html
import json
import re
import subprocess
import tomllib
import unicodedata
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote_to_bytes


class RoadmapError(RuntimeError):
    pass


ROADMAP_MARKER = "<!-- oxide-roadmap-schema:1 -->"
ROADMAP_VIEW_MARKER = "<!-- oxide-roadmap-view:1 -->"
ROADMAP_SCHEMA = 1
OXIDE_VERIFICATION_POLICY_ID = "oxide-verification-policy"
OXIDE_VERIFICATION_POLICY_STATEMENT = (
    "Production logic has meaningful contracts, component refinement, complete coverage, "
    "and exact-tree composition; trusted effects remain narrow and policy-free."
)
DOCUMENT_ROOT_ANCHOR = "oxide://document"
HEADING_LINEAGE_ANCHOR_PREFIX = "oxide://heading/"
LITERAL_HEADING_ANCHOR_PREFIX = "oxide://literal/"
_RESERVED_ANCHOR_PREFIX = "oxide://"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True, slots=True)
class _MarkdownHeading:
    line_index: int
    level: int
    displayed_title: str
    canonical_title: str
    lineage: tuple[tuple[int, str], ...]


def _atx_heading(line: str) -> tuple[int, str] | None:
    """Parse one unquoted ATX heading line using Oxide's ATX-only contract."""
    match = re.fullmatch(r" {0,3}(#{1,6})(?P<rest>(?:[ \t].*)?)", line)
    if match is None:
        return None
    rest = match.group("rest")
    if not rest:
        return len(match.group(1)), ""
    trimmed = rest.rstrip(" \t")
    closing = re.search(r"[ \t]+#+$", trimmed)
    if closing is not None:
        trimmed = trimmed[: closing.start()]
    return len(match.group(1)), trimmed.strip(" \t")


_SOURCE_REFERENCE_VALUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["path", "anchor", "requirement"],
    "properties": {
        "path": {"type": "string", "minLength": 1},
        "anchor": {"type": "string", "minLength": 1},
        "requirement": {"type": "string", "minLength": 1},
    },
}

_GLOBAL_INVARIANT_VALUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["id", "statement", "sources"],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "statement": {"type": "string", "minLength": 1},
        "sources": {
            "type": "array",
            "items": _SOURCE_REFERENCE_VALUE_SCHEMA,
        },
    },
}

_STAGE_VALUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "id",
        "outcome",
        "included_scope",
        "excluded_scope",
        "dependencies",
        "source_specifications",
        "applicable_global_invariants",
        "implementation_goals",
        "verification_goals",
        "readiness",
    ],
    "properties": {
        "id": {"type": "string", "minLength": 1},
        "outcome": {"type": "string", "minLength": 1},
        "included_scope": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "excluded_scope": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "dependencies": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
        },
        "source_specifications": {
            "type": "array",
            "minItems": 1,
            "items": _SOURCE_REFERENCE_VALUE_SCHEMA,
        },
        "applicable_global_invariants": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "implementation_goals": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "verification_goals": {
            "type": "array",
            "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "readiness": {
            "type": "string",
            "enum": ["planned", "ready", "deferred", "blocked"],
        },
    },
}

# This schema governs the transient structured planner response. The authoritative
# repository artifact remains the canonical ROADMAP.md rendered by this module.
ROADMAP_VALUE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "title",
        "status",
        "specification_root",
        "global_invariants",
        "stages",
    ],
    "properties": {
        "schema": {"type": "integer", "enum": [ROADMAP_SCHEMA]},
        "title": {"type": "string", "minLength": 1},
        "status": {"type": "string", "enum": ["draft", "ready"]},
        "specification_root": {"type": "string", "minLength": 1},
        "global_invariants": {
            "type": "array",
            "minItems": 1,
            "items": _GLOBAL_INVARIANT_VALUE_SCHEMA,
        },
        "stages": {
            "type": "array",
            "minItems": 1,
            "items": _STAGE_VALUE_SCHEMA,
        },
    },
}


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _relative_path(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise RoadmapError(f"{field} must be a nonempty relative path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise RoadmapError(f"{field} must not escape the target repository")
    return path.as_posix()


def _strings(value: object, field: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise RoadmapError(f"{field} must be a list of nonempty strings")
    if nonempty and not value:
        raise RoadmapError(f"{field} must not be empty")
    if len(set(value)) != len(value):
        raise RoadmapError(f"{field} contains duplicates")
    return list(value)


def _source_references(value: object, field: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise RoadmapError(f"{field} must contain at least one source requirement")
    result: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for ordinal, item in enumerate(value, 1):
        if not isinstance(item, dict) or set(item) != {"path", "anchor", "requirement"}:
            raise RoadmapError(
                f"{field}[{ordinal}] must contain exactly path, anchor, and requirement"
            )
        path = _relative_path(item["path"], f"{field}[{ordinal}].path")
        anchor, requirement = item["anchor"], item["requirement"]
        if (
            not isinstance(anchor, str)
            or not anchor.strip()
            or not isinstance(requirement, str)
            or not requirement.strip()
            or any(character in anchor for character in "\x00\r\n")
            or "\x00" in requirement
        ):
            raise RoadmapError(f"{field}[{ordinal}] has malformed source text")
        try:
            normalized_anchor = validate_source_anchor(anchor)
        except ValueError as error:
            raise RoadmapError(f"{field}[{ordinal}] has malformed source text") from error
        preserved_requirement = (
            unicodedata.normalize("NFC", requirement).replace("\r\n", "\n").replace("\r", "\n")
        )
        normalized = (path, normalized_anchor, canonical_source_text(preserved_requirement))
        if normalized in seen:
            raise RoadmapError(f"{field} contains duplicate source requirements")
        seen.add(normalized)
        result.append(
            {
                "path": normalized[0],
                "anchor": normalized[1],
                "requirement": preserved_requirement,
            }
        )
    return sorted(
        result,
        key=lambda reference: (
            reference["path"],
            reference["anchor"],
            canonical_source_text(reference["requirement"]),
        ),
    )


def _invariant_order(identifier: str) -> tuple[int, str]:
    """Keep Oxide's universal policy first, then use stable invariant identity."""
    return (0 if identifier == OXIDE_VERIFICATION_POLICY_ID else 1, identifier)


def _validate_verification_policy(roadmap: dict[str, Any]) -> None:
    policy = [
        invariant
        for invariant in roadmap["global_invariants"]
        if invariant["id"] == OXIDE_VERIFICATION_POLICY_ID
    ]
    source_free = [
        invariant for invariant in roadmap["global_invariants"] if not invariant["sources"]
    ]
    if (
        len(policy) != 1
        or policy[0]["statement"] != OXIDE_VERIFICATION_POLICY_STATEMENT
        or policy[0]["sources"] != []
        or source_free != policy
    ):
        raise RoadmapError(
            "roadmap must declare exactly one source-free oxide-verification-policy "
            "invariant with the mandated statement"
        )
    for stage in roadmap["stages"]:
        if OXIDE_VERIFICATION_POLICY_ID not in stage["applicable_global_invariants"]:
            raise RoadmapError(f"phase {stage['id']!r} does not apply oxide-verification-policy")


def _canonical_stage_order(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order phases by dependency layer without adding artificial dependencies."""
    by_id = {stage["id"]: stage for stage in stages}
    layers: dict[str, int] = {}
    remaining = set(by_id)
    while remaining:
        ready = sorted(
            identifier
            for identifier in remaining
            if set(by_id[identifier]["dependencies"]) <= layers.keys()
        )
        if not ready:
            raise RoadmapError("roadmap stage dependency graph contains a cycle")
        for identifier in ready:
            dependencies = by_id[identifier]["dependencies"]
            layers[identifier] = (
                1 + max(layers[dependency] for dependency in dependencies) if dependencies else 0
            )
            remaining.remove(identifier)
    return sorted(stages, key=lambda stage: (layers[stage["id"]], stage["id"]))


def _roadmap_toml_text(text: str, source: str | Path) -> str:
    marker = text.find(ROADMAP_MARKER)
    if marker < 0:
        raise RoadmapError(f"roadmap lacks {ROADMAP_MARKER}: {source}")
    fence = re.search(r"```toml[ \t]*\n(.*?)\n```", text[marker:], re.DOTALL)
    if fence is None:
        raise RoadmapError(f"roadmap lacks one TOML contract block after its marker: {source}")
    return fence.group(1).strip()


def _roadmap_toml(text: str, source: str | Path) -> dict[str, Any]:
    try:
        value = tomllib.loads(_roadmap_toml_text(text, source))
    except tomllib.TOMLDecodeError as error:
        raise RoadmapError(f"roadmap TOML is invalid: {error}") from error
    return value


def validate_roadmap(value: object, source: str | Path = "ROADMAP.md") -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RoadmapError("roadmap schema must be an object")
    allowed = {
        "schema",
        "title",
        "status",
        "specification_root",
        "global_invariants",
        "stages",
    }
    if set(value) != allowed:
        raise RoadmapError(
            f"roadmap has unsupported or missing fields: {sorted(set(value) ^ allowed)}"
        )
    if value.get("schema") != ROADMAP_SCHEMA:
        raise RoadmapError("unsupported roadmap schema")
    title, status = value.get("title"), value.get("status")
    if not isinstance(title, str) or not title.strip():
        raise RoadmapError("roadmap.title must be nonempty")
    if status not in {"draft", "ready"}:
        raise RoadmapError("roadmap.status must be draft or ready")
    specification_root = _relative_path(
        value.get("specification_root"), "roadmap.specification_root"
    )
    raw_invariants = value.get("global_invariants")
    if not isinstance(raw_invariants, list):
        raise RoadmapError("roadmap.global_invariants must be a list")
    invariants: list[dict[str, Any]] = []
    invariant_ids: set[str] = set()
    for ordinal, raw in enumerate(raw_invariants, 1):
        if not isinstance(raw, dict) or set(raw) != {"id", "statement", "sources"}:
            raise RoadmapError(f"global invariant {ordinal} has the wrong fields")
        identifier, statement = raw.get("id"), raw.get("statement")
        if (
            not isinstance(identifier, str)
            or _SAFE_ID.fullmatch(identifier) is None
            or identifier in invariant_ids
            or not isinstance(statement, str)
            or not statement.strip()
        ):
            raise RoadmapError(f"global invariant {ordinal} is malformed or duplicate")
        invariant_ids.add(identifier)
        sources = raw.get("sources")
        if not isinstance(sources, list):
            raise RoadmapError(f"global invariant {identifier}.sources must be a list")
        invariants.append(
            {
                "id": identifier,
                "statement": statement.strip(),
                "sources": (
                    _source_references(sources, f"global invariant {identifier}.sources")
                    if sources
                    else []
                ),
            }
        )
    invariants.sort(key=lambda invariant: _invariant_order(invariant["id"]))
    raw_stages = value.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise RoadmapError("roadmap.stages must contain at least one stage")
    stage_ids: set[str] = set()
    stages: list[dict[str, Any]] = []
    stage_fields = {
        "id",
        "outcome",
        "included_scope",
        "excluded_scope",
        "dependencies",
        "source_specifications",
        "applicable_global_invariants",
        "implementation_goals",
        "verification_goals",
        "readiness",
    }
    for ordinal, raw in enumerate(raw_stages, 1):
        if not isinstance(raw, dict) or set(raw) != stage_fields:
            actual = set(raw) if isinstance(raw, dict) else set()
            unsupported = sorted(actual - stage_fields)
            missing = sorted(stage_fields - actual)
            raise RoadmapError(
                f"roadmap stage {ordinal} has unsupported fields {unsupported} "
                f"and missing fields {missing}"
            )
        identifier, outcome = raw.get("id"), raw.get("outcome")
        if (
            not isinstance(identifier, str)
            or _SAFE_ID.fullmatch(identifier) is None
            or identifier in stage_ids
            or not isinstance(outcome, str)
            or not outcome.strip()
        ):
            raise RoadmapError(f"roadmap stage {ordinal} is malformed or duplicate")
        stage_ids.add(identifier)
        applicable = sorted(
            _strings(
                raw.get("applicable_global_invariants"),
                f"stage {identifier}.applicable_global_invariants",
            ),
            key=_invariant_order,
        )
        if not set(applicable) <= invariant_ids:
            raise RoadmapError(f"stage {identifier} names an unknown global invariant")
        readiness = raw.get("readiness")
        if readiness not in {"planned", "ready", "deferred", "blocked"}:
            raise RoadmapError(f"stage {identifier}.readiness is invalid")
        stages.append(
            {
                "id": identifier,
                "outcome": outcome.strip(),
                "included_scope": _strings(
                    raw.get("included_scope"), f"stage {identifier}.included_scope", nonempty=True
                ),
                "excluded_scope": _strings(
                    raw.get("excluded_scope"), f"stage {identifier}.excluded_scope"
                ),
                "dependencies": sorted(
                    _strings(raw.get("dependencies"), f"stage {identifier}.dependencies")
                ),
                "source_specifications": _source_references(
                    raw.get("source_specifications"),
                    f"stage {identifier}.source_specifications",
                ),
                "applicable_global_invariants": applicable,
                "implementation_goals": _strings(
                    raw.get("implementation_goals"),
                    f"stage {identifier}.implementation_goals",
                    nonempty=True,
                ),
                "verification_goals": _strings(
                    raw.get("verification_goals"),
                    f"stage {identifier}.verification_goals",
                    nonempty=True,
                ),
                "readiness": readiness,
            }
        )
    known = {stage["id"] for stage in stages}
    for stage in stages:
        dependencies = set(stage["dependencies"])
        if stage["id"] in dependencies or not dependencies <= known:
            raise RoadmapError(f"stage {stage['id']} has invalid dependencies")
    stages = _canonical_stage_order(stages)
    normalized = {
        "schema": ROADMAP_SCHEMA,
        "title": title.strip(),
        "status": status,
        "specification_root": specification_root,
        "global_invariants": invariants,
        "stages": stages,
    }
    json.dumps(normalized, ensure_ascii=False, sort_keys=True, allow_nan=False)
    return normalized


def parse_roadmap(text: str, source: str | Path = "ROADMAP.md") -> dict[str, Any]:
    return validate_roadmap(_roadmap_toml(text, source), source)


def roadmap_maintenance_impact(
    baseline: dict[str, Any], proposed: dict[str, Any], stage_ids: Iterable[str]
) -> dict[str, Any]:
    """Validate a scoped roadmap update and report its approval impact."""
    requested = list(stage_ids)
    if not requested or len(requested) != len(set(requested)):
        raise RoadmapError("maintenance must select one or more unique phase IDs")
    baseline_ids = [stage["id"] for stage in baseline["stages"]]
    proposed_ids = [stage["id"] for stage in proposed["stages"]]
    if baseline_ids != proposed_ids:
        raise RoadmapError("maintenance cannot add, remove, rename, or reorder phases")
    unknown = sorted(set(requested) - set(baseline_ids))
    if unknown:
        raise RoadmapError(f"maintenance selected unknown phases: {', '.join(unknown)}")
    for field in (
        "schema",
        "title",
        "status",
        "specification_root",
        "global_invariants",
    ):
        if baseline[field] != proposed[field]:
            raise RoadmapError(f"maintenance cannot change roadmap.{field}")

    current = {stage["id"]: stage for stage in baseline["stages"]}
    replacement = {stage["id"]: stage for stage in proposed["stages"]}
    changes: list[dict[str, Any]] = []
    semantic_changes: set[str] = set()
    for identifier in baseline_ids:
        before, after = current[identifier], replacement[identifier]
        changed_fields = sorted(
            field for field in before if field != "id" and before[field] != after[field]
        )
        if not changed_fields:
            continue
        if identifier not in requested:
            raise RoadmapError(
                f"maintenance changed unselected phase {identifier!r}; select it explicitly"
            )
        changes.append({"stage_id": identifier, "fields": changed_fields})
        if set(changed_fields) - {"readiness"}:
            semantic_changes.add(identifier)

    def descendants(stages: list[dict[str, Any]], roots: set[str]) -> set[str]:
        found = set(roots)
        changed = True
        while changed:
            changed = False
            for stage in stages:
                if stage["id"] not in found and set(stage["dependencies"]) & found:
                    found.add(stage["id"])
                    changed = True
        return found - roots

    dependent = descendants(baseline["stages"], semantic_changes) | descendants(
        proposed["stages"], semantic_changes
    )
    changed_ids = {change["stage_id"] for change in changes}
    invalidated = changed_ids | dependent
    order = {identifier: ordinal for ordinal, identifier in enumerate(baseline_ids)}
    return {
        "selected_stage_ids": requested,
        "changes": changes,
        "dependent_stage_ids": sorted(dependent, key=order.__getitem__),
        "invalidated_stage_ids": sorted(invalidated, key=order.__getitem__),
        "preserved_stage_ids": [
            identifier for identifier in baseline_ids if identifier not in invalidated
        ],
    }


def _plain_markdown(value: object) -> str:
    """Render schema prose as one literal Markdown line."""
    rendered = html.escape(re.sub(r"\s+", " ", str(value)).strip(), quote=False)
    for character in ("\\", "`", "*", "_", "[", "]"):
        rendered = rendered.replace(character, "\\" + character)
    return rendered


def _table_text(value: object) -> str:
    return _plain_markdown(value).replace("|", "\\|")


def _canonical_heading_slug(value: str) -> str:
    normalized = value.lower()
    normalized = re.sub(r"[^\w\s-]", "", normalized)
    return re.sub(r"[\s-]+", "-", normalized).strip("-")


def _heading_slug(value: str) -> str:
    return _canonical_heading_slug(_normalize_heading(value))


def _source_links(references: Iterable[dict[str, str]]) -> list[str]:
    links: list[str] = []
    seen: set[tuple[str, str]] = set()
    for reference in references:
        identity = (reference["path"], reference["anchor"])
        if identity in seen:
            continue
        seen.add(identity)
        path, anchor = identity
        target = quote(path, safe="/._-~")
        if anchor == DOCUMENT_ROOT_ANCHOR:
            anchor_label = "document root"
        else:
            lineage = _parse_heading_lineage_anchor(anchor)
            literal_title = _parse_literal_heading_anchor(anchor)
            if literal_title is not None:
                anchor_label = literal_title or "(empty heading)"
                slug = _heading_slug(literal_title)
                if slug:
                    target += "#" + quote(slug, safe="-_")
            elif lineage is None:
                anchor_label = anchor
                slug = _canonical_heading_slug(anchor)
                if slug:
                    target += "#" + quote(slug, safe="-_")
            else:
                anchor_label = " › ".join(
                    (
                        f"{title or '(empty heading)'} [{occurrence}]"
                        if occurrence > 1
                        else title or "(empty heading)"
                    )
                    for occurrence, title in lineage
                )
        label = f"{Path(path).name} — {anchor_label}"
        links.append(f"[{_plain_markdown(label)}]({target})")
    return links


def _code_list(values: Iterable[str], *, empty: str = "None") -> str:
    rendered = [f"`{_plain_markdown(value)}`" for value in values]
    return ", ".join(rendered) if rendered else empty


def _phase_anchor(identifier: str) -> str:
    return "phase-" + re.sub(r"[^A-Za-z0-9_-]", "-", identifier)


def _append_bullets(lines: list[str], values: Iterable[str]) -> None:
    lines.extend(f"- {_plain_markdown(value)}" for value in values)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _append_toml_array(
    lines: list[str], key: str, values: Iterable[Any], render: Callable[[Any], str]
) -> None:
    items = list(values)
    if not items:
        lines.append(f"{key} = []")
        return
    lines.append(f"{key} = [")
    lines.extend(f"  {render(item)}," for item in items)
    lines.append("]")


def _toml_source(reference: dict[str, str]) -> str:
    return (
        "{ path = "
        + _toml_string(reference["path"])
        + ", anchor = "
        + _toml_string(reference["anchor"])
        + ", requirement = "
        + _toml_string(reference["requirement"])
        + " }"
    )


def _canonical_roadmap_toml(roadmap: dict[str, Any]) -> str:
    lines = [
        f"schema = {ROADMAP_SCHEMA}",
        f"title = {_toml_string(roadmap['title'])}",
        f"status = {_toml_string(roadmap['status'])}",
        f"specification_root = {_toml_string(roadmap['specification_root'])}",
    ]
    if not roadmap["global_invariants"]:
        lines.append("global_invariants = []")
    for invariant in roadmap["global_invariants"]:
        lines.extend(
            [
                "",
                "[[global_invariants]]",
                f"id = {_toml_string(invariant['id'])}",
                f"statement = {_toml_string(invariant['statement'])}",
            ]
        )
        _append_toml_array(lines, "sources", invariant["sources"], _toml_source)
    for stage in roadmap["stages"]:
        lines.extend(
            [
                "",
                "[[stages]]",
                f"id = {_toml_string(stage['id'])}",
                f"outcome = {_toml_string(stage['outcome'])}",
            ]
        )
        for key in ("included_scope", "excluded_scope", "dependencies"):
            _append_toml_array(lines, key, stage[key], _toml_string)
        _append_toml_array(
            lines,
            "source_specifications",
            stage["source_specifications"],
            _toml_source,
        )
        for key in (
            "applicable_global_invariants",
            "implementation_goals",
            "verification_goals",
        ):
            _append_toml_array(lines, key, stage[key], _toml_string)
        lines.append(f"readiness = {_toml_string(stage['readiness'])}")
    return "\n".join(lines)


def _human_roadmap(roadmap: dict[str, Any]) -> str:
    status = str(roadmap["status"]).replace("_", " ").title()
    specification_root = roadmap["specification_root"].rstrip("/") + "/"
    specification_target = quote(specification_root, safe="/._-~")
    lines = [
        ROADMAP_VIEW_MARKER,
        "# Roadmap",
        "",
        f"> **Plan status:** {status}  ",
        f"> **Specifications:** [{_plain_markdown(specification_root)}]({specification_target})",
        "",
        "## At a glance",
        "",
        "| Phase | Outcome | Readiness | Depends on |",
        "| --- | --- | --- | --- |",
    ]
    for stage in roadmap["stages"]:
        identifier = stage["id"]
        readiness = str(stage["readiness"]).replace("_", " ").title()
        dependencies = _code_list(stage["dependencies"])
        lines.append(
            f"| [`{_table_text(identifier)}`](#{_phase_anchor(identifier)}) | "
            f"{_table_text(stage['outcome'])} | {readiness} | {dependencies} |"
        )

    lines.extend(["", "## Global invariants", ""])
    if not roadmap["global_invariants"]:
        lines.extend(["No global invariants are declared.", ""])
    for invariant in roadmap["global_invariants"]:
        sources = _source_links(invariant["sources"])
        lines.extend(
            [
                f"### `{_plain_markdown(invariant['id'])}`",
                "",
                _plain_markdown(invariant["statement"]),
                "",
                (
                    "**Sources:** " + ", ".join(sources)
                    if sources
                    else "**Authority:** Governing verification policy"
                ),
                "",
            ]
        )

    lines.extend(["## Implementation phases", ""])
    for ordinal, stage in enumerate(roadmap["stages"], 1):
        identifier = stage["id"]
        readiness = str(stage["readiness"]).replace("_", " ").title()
        lines.extend(
            [
                f'<a id="{_phase_anchor(identifier)}"></a>',
                (
                    f"### {ordinal}. `{_plain_markdown(identifier)}` — "
                    f"{_plain_markdown(stage['outcome'])}"
                ),
                "",
                f"**Readiness:** {readiness}  ",
                f"**Depends on:** {_code_list(stage['dependencies'])}  ",
                "**Global invariants:** " + _code_list(stage["applicable_global_invariants"]),
                "",
                "#### Scope",
                "",
            ]
        )
        _append_bullets(lines, stage["included_scope"])
        if stage["excluded_scope"]:
            lines.extend(["", "#### Deferred or excluded", ""])
            _append_bullets(lines, stage["excluded_scope"])
        if stage["implementation_goals"] != stage["included_scope"]:
            lines.extend(["", "#### Implementation goals", ""])
            _append_bullets(lines, stage["implementation_goals"])
        lines.extend(["", "#### Verification goals", ""])
        _append_bullets(lines, stage["verification_goals"])
        lines.extend(
            [
                "",
                "#### Sources",
                "",
                *(f"- {link}" for link in _source_links(stage["source_specifications"])),
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def _render_validated_roadmap(roadmap: dict[str, Any]) -> str:
    toml_text = _canonical_roadmap_toml(roadmap)
    return (
        _human_roadmap(roadmap)
        + "\n\n## Machine-readable roadmap data\n\n"
        + "The data below is authoritative. The readable sections above are generated "
        + "from it by the planning system.\n\n"
        + "<details>\n"
        + "<summary>Show authoritative TOML</summary>\n\n"
        + ROADMAP_MARKER
        + "\n```toml\n"
        + toml_text
        + "\n```\n\n</details>\n"
    )


def render_roadmap_value(value: object, source: str | Path = "ROADMAP.md") -> str:
    """Render one structured roadmap value into the canonical repository artifact."""
    return _render_validated_roadmap(validate_roadmap(value, source))


def render_roadmap_document(text: str, source: str | Path = "ROADMAP.md") -> str:
    """Create the canonical human view around the authoritative TOML schema.

    Agent-authored prose outside the schema block is intentionally discarded. This
    leaves one semantic representation: the validated TOML. The readable Markdown
    is a deterministic projection and therefore cannot drift independently.
    """
    return _render_validated_roadmap(parse_roadmap(text, source))


def load_roadmap(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        return parse_roadmap(source.read_text(encoding="utf-8"), source)
    except (OSError, UnicodeDecodeError) as error:
        raise RoadmapError(f"cannot read roadmap {source}: {error}") from error


def _canonical_heading_title(value: str) -> str:
    """Normalize Markdown heading presentation while preserving its title."""
    value = value.strip()
    if not value:
        return ""
    # Prefix with a non-list token so a numeric heading such as ``14.3`` is not
    # mistaken for ordered-list presentation by the general Markdown normalizer.
    sentinel = "\ue002"
    canonical = canonical_source_text(f"{sentinel} {value}")
    if canonical == sentinel:
        return ""
    return canonical.removeprefix(f"{sentinel} ")


def _canonical_legacy_anchor(value: str) -> str:
    """Retain compatibility with callers that supplied a complete ATX line."""
    stripped = value.strip()
    heading = _atx_heading(stripped)
    return _canonical_heading_title(heading[1] if heading is not None else stripped)


def _decode_canonical_anchor_segment(value: str) -> str:
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise ValueError("source anchor has malformed URL encoding")
    try:
        decoded = unquote_to_bytes(value).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("source anchor must be valid UTF-8") from error
    if any(character in decoded for character in "\x00\r\n"):
        raise ValueError("source anchor contains an invalid character")
    if quote(decoded, safe="-._~") != value:
        raise ValueError("source anchor must use canonical URL encoding")
    return decoded


def _parse_heading_lineage_anchor(value: str) -> tuple[tuple[int, str], ...] | None:
    """Decode an explicit, occurrence-qualified root-to-heading lineage."""
    if not value.startswith(HEADING_LINEAGE_ANCHOR_PREFIX):
        return None
    parts = value.removeprefix(HEADING_LINEAGE_ANCHOR_PREFIX).split("/")
    if len(parts) < 2 or len(parts) % 2:
        raise ValueError("heading lineage must contain occurrence/title pairs")
    lineage: list[tuple[int, str]] = []
    for index in range(0, len(parts), 2):
        occurrence_text, encoded_title = parts[index : index + 2]
        if re.fullmatch(r"[1-9][0-9]{0,8}", occurrence_text) is None:
            raise ValueError("heading occurrence must be a positive decimal")
        decoded_title = _decode_canonical_anchor_segment(encoded_title)
        if unicodedata.normalize("NFC", decoded_title) != decoded_title:
            raise ValueError("heading lineage titles must use canonical Unicode")
        # A lineage segment is already a canonical semantic title. Applying
        # the Markdown projection again would corrupt valid titles such as
        # ``&amp;`` (the one-pass projection of ``&amp;amp;``).
        lineage.append((int(occurrence_text), decoded_title))
    parsed = tuple(lineage)
    if _format_heading_lineage_anchor(parsed) != value:
        raise ValueError("heading lineage must use its canonical URL encoding")
    return parsed


def _format_heading_lineage_anchor(lineage: Iterable[tuple[int, str]]) -> str:
    components = [
        component
        for occurrence, title in lineage
        for component in (str(occurrence), quote(title, safe="-._~"))
    ]
    return HEADING_LINEAGE_ANCHOR_PREFIX + "/".join(components)


def _parse_literal_heading_anchor(value: str) -> str | None:
    """Decode a collision-free escape for one unique literal heading title."""
    if not value.startswith(LITERAL_HEADING_ANCHOR_PREFIX):
        return None
    encoded_title = value.removeprefix(LITERAL_HEADING_ANCHOR_PREFIX)
    return _decode_canonical_anchor_segment(encoded_title)


def validate_source_anchor(value: str) -> str:
    """Validate locator syntax while preserving an ordinary canonical title."""
    if any(character in value for character in "\x00\r\n"):
        raise ValueError("source anchor contains an invalid character")
    if value == DOCUMENT_ROOT_ANCHOR:
        return value
    if value.startswith(HEADING_LINEAGE_ANCHOR_PREFIX):
        _parse_heading_lineage_anchor(value)
        return value
    if value.startswith(LITERAL_HEADING_ANCHOR_PREFIX):
        _parse_literal_heading_anchor(value)
        return value
    if value.startswith(_RESERVED_ANCHOR_PREFIX) or value.strip().startswith(
        _RESERVED_ANCHOR_PREFIX
    ):
        raise ValueError("source anchor uses a malformed reserved Oxide locator")
    ordinary = unicodedata.normalize("NFC", value.strip())
    if not ordinary:
        raise ValueError("empty heading titles require an explicit Oxide locator")
    return ordinary


def canonical_source_anchor(value: str) -> str:
    """Project legacy Markdown presentation to a source-title comparison key.

    Production resolution first treats an ordinary anchor as already-canonical
    data and calls this projection only as a source-aware compatibility fallback.
    Keeping the projection separate prevents a canonical title such as
    ``# Rules`` or ``&amp;`` from being interpreted twice.
    """
    anchor = validate_source_anchor(value)
    if anchor.startswith(_RESERVED_ANCHOR_PREFIX):
        return anchor
    legacy_anchor = _canonical_legacy_anchor(anchor)
    if not legacy_anchor:
        raise ValueError("empty heading titles require an explicit Oxide locator")
    if legacy_anchor.startswith(_RESERVED_ANCHOR_PREFIX):
        raise ValueError("source anchor canonicalizes into the reserved Oxide namespace")
    return legacy_anchor


def _normalize_heading(value: str) -> str:
    return _canonical_heading_title(value)


def _protect_markdown_code(value: str) -> tuple[str, list[tuple[str, str]]]:
    """Protect code spans while preserving their kind and bytes exactly."""
    protected: list[tuple[str, str]] = []

    def token(kind: str, content: str) -> str:
        index = len(protected)
        protected.append((kind, content))
        return f"\ue000{index}\ue001"

    lines = value.split("\n")
    rendered: list[str] = []
    index = 0
    while index < len(lines):
        opener = _opening_markdown_fence(lines[index].removesuffix("\r"))
        if opener is None:
            rendered.append(lines[index])
            index += 1
            continue
        closing = index + 1
        while closing < len(lines) and not _closes_markdown_fence(
            lines[closing].removesuffix("\r"), opener
        ):
            closing += 1
        content_end = min(len(lines), closing)
        rendered.append(token("fenced", "\n".join(lines[index + 1 : content_end])))
        index = min(len(lines), closing + 1)
    value = "\n".join(rendered)
    value = re.sub(
        r"(?<!`)`([^`\n]*)`(?!`)",
        lambda match: token("inline", match.group(1)),
        value,
    )
    return value, protected


def _restore_markdown_code(value: str, protected: list[tuple[str, str]]) -> str:
    for index, (kind, content) in enumerate(protected):
        canonical = f"```\n{content}\n```" if kind == "fenced" else f"`{content}`"
        value = value.replace(f"\ue000{index}\ue001", canonical)
    return value


def _canonical_inline_markdown(value: str) -> str:
    """Normalize presentation-only inline Markdown around semantic text."""
    value = re.sub(
        r"</?(?:b|em|i|mark|small|span|strong|sub|sup|u)(?:\s[^>]*)?>",
        "",
        value,
        flags=re.IGNORECASE,
    )
    # Retain link destinations because changing one can change meaning; only
    # the Markdown punctuation around the label is presentational.
    value = re.sub(r"!\[([^]]*)\]\(([^)]+)\)", r"\1 \2", value)
    value = re.sub(r"\[([^]]+)\]\(([^)]+)\)", r"\1 \2", value)
    for delimiters in (("**", "**"), ("__", "__"), ("*", "*"), ("_", "_")):
        opening, closing = map(re.escape, delimiters)
        value = re.sub(
            rf"(?<!\w){opening}(?=\S)(.+?)(?<=\S){closing}(?!\w)",
            r"\1",
            value,
        )
    value = re.sub(r"\\([\\`*{}\[\]()#+\-.!_>])", r"\1", value)
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _blockquote_prefix(value: str) -> tuple[int, str]:
    """Return Markdown blockquote depth and the text after its markers."""
    original = value
    depth = 0
    leading = re.match(r"^[ \t]{0,3}(?=>)", value)
    remainder = value[leading.end() :] if leading else value
    if not remainder.startswith(">"):
        return 0, original
    while remainder.startswith(">"):
        depth += 1
        remainder = remainder[1:]
        if remainder.startswith((" ", "\t")):
            remainder = remainder[1:]
    return depth, remainder


def _list_depth(stack: list[int], indentation: int) -> int:
    """Normalize arbitrary indentation widths to relative list nesting."""
    if not stack:
        stack.append(indentation)
        return 0
    if indentation > stack[-1]:
        stack.append(indentation)
        return len(stack) - 1
    while len(stack) > 1 and indentation < stack[-1]:
        stack.pop()
    if indentation > stack[-1]:
        stack.append(indentation)
    elif indentation < stack[-1]:
        stack[-1] = indentation
    return len(stack) - 1


def _canonical_table_row(value: str) -> tuple[str, int] | None:
    """Return one normalized Markdown table row and its column count."""
    if not re.search(r"(?<!\\)\|", value):
        return None
    cells = re.split(r"(?<!\\)\|", value.strip())
    if cells and not cells[0].strip():
        cells.pop(0)
    if cells and not cells[-1].strip():
        cells.pop()
    if not cells:
        return None
    return (
        "| " + " | ".join(_canonical_inline_markdown(cell) for cell in cells) + " |",
        len(cells),
    )


def canonical_source_text(value: str) -> str:
    """Canonicalize Markdown presentation while retaining semantic source text.

    Case, words, punctuation, links, code bytes and boundaries, paragraph/table/
    blockquote boundaries, list nesting, ordered-list ordinals, and
    task-checkbox state remain significant.
    Soft wrapping, cosmetic indentation widths, equivalent list-marker
    spellings, heading markers, emphasis, escapes, comments, and table-rule
    styling do not. This lets exact source citations survive a formatter without
    allowing a semantic rewrite to retain authority.
    """
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    normalized, protected = _protect_markdown_code(normalized)
    normalized = re.sub(r"<!--[\s\S]*?-->", "", normalized)
    blocks: list[dict[str, object]] = []
    table_rule = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$")
    horizontal_rule = re.compile(r"^(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$")
    list_marker = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>[-+*]|\d{1,9}[.)])[ \t]+(?P<body>.*)$")
    list_stack: list[int] = []
    active_quote_depth: int | None = None
    separated = True
    raw_lines = normalized.split("\n")
    table_rows: set[int] = set()
    table_headers: set[int] = set()
    for index in range(len(raw_lines) - 1):
        quote_depth, candidate = _blockquote_prefix(raw_lines[index])
        rule_quote_depth, rule = _blockquote_prefix(raw_lines[index + 1])
        if (
            quote_depth != rule_quote_depth
            or _canonical_table_row(candidate.strip()) is None
            or table_rule.fullmatch(rule.strip()) is None
        ):
            continue
        table_headers.add(index)
        table_rows.add(index)
        cursor = index + 2
        while cursor < len(raw_lines):
            row_quote_depth, row = _blockquote_prefix(raw_lines[cursor])
            if row_quote_depth != quote_depth or _canonical_table_row(row.strip()) is None:
                break
            table_rows.add(cursor)
            cursor += 1

    for index, raw_line in enumerate(raw_lines):
        quote_depth, quoted_line = _blockquote_prefix(raw_line)
        heading = _atx_heading(quoted_line.rstrip(" \t"))
        line = heading[1] if heading is not None else quoted_line.strip()
        if not line:
            separated = True
            list_stack.clear()
            active_quote_depth = None
            continue
        if table_rule.fullmatch(line) or horizontal_rule.fullmatch(line):
            continue
        if heading is not None:
            list_stack.clear()
            separated = True

        protected_token = re.fullmatch(r"\ue000(\d+)\ue001", line)
        if protected_token and protected[int(protected_token.group(1))][0] == "fenced":
            blocks.append(
                {
                    "kind": "code",
                    "quote_depth": quote_depth,
                    "prefix": ("> " * quote_depth).rstrip(),
                    "text": line,
                    "separated_before": separated,
                }
            )
            list_stack.clear()
            active_quote_depth = None
            separated = False
            continue

        table_row = _canonical_table_row(line) if index in table_rows else None
        if table_row is not None:
            row, columns = table_row
            blocks.append(
                {
                    "kind": "table",
                    "quote_depth": quote_depth,
                    "prefix": ("> " * quote_depth).rstrip(),
                    "text": row,
                    "columns": columns,
                    "header": index in table_headers,
                    "separated_before": separated,
                }
            )
            list_stack.clear()
            active_quote_depth = None
            separated = False
            continue

        match = list_marker.match(quoted_line)
        if match:
            if active_quote_depth != quote_depth:
                list_stack.clear()
            active_quote_depth = quote_depth
            indentation = len(match.group("indent").expandtabs(4))
            depth = _list_depth(list_stack, indentation)
            marker = match.group("marker")
            body = match.group("body")
            checkbox = re.match(r"^\[([ xX])\][ \t]+(.*)$", body)
            task_state = ""
            if checkbox:
                task_state = "[x] " if checkbox.group(1).lower() == "x" else "[ ] "
                body = checkbox.group(2)
            canonical_body = _canonical_inline_markdown(body)
            if marker[0].isdigit():
                canonical_marker = f"{int(marker[:-1])}. "
            else:
                canonical_marker = "- "
            prefix = f"{'> ' * quote_depth}{'  ' * depth}{canonical_marker}{task_state}"
            blocks.append(
                {
                    "kind": "list",
                    "quote_depth": quote_depth,
                    "prefix": prefix,
                    "text": canonical_body,
                    "separated_before": separated,
                }
            )
            separated = False
            continue

        canonical_line = _canonical_inline_markdown(line)
        if not canonical_line:
            continue
        # A lazily wrapped list item is part of the preceding item regardless of
        # whether a formatter indents the continuation by two or four spaces.
        if (
            not separated
            and blocks
            and blocks[-1]["kind"] == "list"
            and blocks[-1]["quote_depth"] == quote_depth
        ):
            blocks[-1]["text"] = f"{blocks[-1]['text']} {canonical_line}".strip()
            separated = False
            continue
        else:
            prefix = ("> " * quote_depth).rstrip()
            if (
                not separated
                and blocks
                and blocks[-1]["kind"] == "prose"
                and blocks[-1]["quote_depth"] == quote_depth
            ):
                blocks[-1]["text"] = f"{blocks[-1]['text']} {canonical_line}".strip()
            else:
                blocks.append(
                    {
                        "kind": "prose",
                        "quote_depth": quote_depth,
                        "prefix": prefix,
                        "text": canonical_line,
                        "separated_before": separated,
                    }
                )
        list_stack.clear()
        active_quote_depth = None
        separated = False
    rendered: list[str] = []
    for block in blocks:
        if rendered and block["separated_before"]:
            rendered.append("")
        if block["kind"] == "list":
            rendered.append(f"{block['prefix']}{block['text']}")
        else:
            rendered.append(f"{block['prefix']} {block['text']}".strip())
            if block["kind"] == "table" and block["header"]:
                delimiter = "| " + " | ".join("---" for _ in range(block["columns"])) + " |"
                rendered.append(f"{block['prefix']} {delimiter}".strip())
    canonical = "\n".join(rendered).strip()
    return _restore_markdown_code(canonical, protected)


def _citation_markdown_regions(value: str) -> tuple[list[str], bool]:
    """Split Markdown at headings without mistaking fenced code for headings."""
    lines = value.splitlines()
    heading_lines = {entry.line_index for entry in _markdown_heading_entries(lines)}
    regions: list[str] = []
    current: list[str] = []
    for index, line in enumerate(lines):
        if index in heading_lines:
            region = "\n".join(current).strip()
            if region:
                regions.append(region)
            current = []
        else:
            current.append(line)
    region = "\n".join(current).strip()
    if region:
        regions.append(region)
    return regions, bool(heading_lines)


def _citation_code_tokens(value: str) -> tuple[str, list[tuple[str, str]]]:
    """Encode code kind and bytes as whitespace-insensitive comparison tokens."""
    protected_value, protected = _protect_markdown_code(value)
    for index, (kind, content) in enumerate(protected):
        token = f"\ue100{kind}:{content.encode('utf-8').hex()}\ue101"
        protected_value = protected_value.replace(f"\ue000{index}\ue001", token)
    return protected_value, protected


def _citation_units(
    value: str, *, canonical_input: bool = False
) -> list[tuple[tuple[object, ...], str]]:
    """Project one heading region into semantically scoped citation units."""
    canonical, _ = _citation_code_tokens(value if canonical_input else canonical_source_text(value))
    raw_lines = canonical.splitlines()
    table_rule = re.compile(r"^\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?$")
    table_rows: set[int] = set()
    table_headers: set[int] = set()
    for index in range(len(raw_lines) - 1):
        quote_depth, candidate = _blockquote_prefix(raw_lines[index])
        rule_quote_depth, rule = _blockquote_prefix(raw_lines[index + 1])
        if (
            quote_depth != rule_quote_depth
            or _canonical_table_row(candidate.strip()) is None
            or table_rule.fullmatch(rule.strip()) is None
        ):
            continue
        table_headers.add(index)
        table_rows.add(index)
        cursor = index + 2
        while cursor < len(raw_lines):
            row_quote_depth, row = _blockquote_prefix(raw_lines[cursor])
            if row_quote_depth != quote_depth or _canonical_table_row(row.strip()) is None:
                break
            table_rows.add(cursor)
            cursor += 1

    units: list[tuple[tuple[object, ...], str]] = []
    list_marker = re.compile(r"^(?P<indent>[ \t]*)(?P<marker>-|\d{1,9}\.)[ \t]+(?P<body>.*)$")

    def append(structure: tuple[object, ...], text: str) -> None:
        text = re.sub(r"[ \t]+", " ", text).strip()
        if not text:
            return
        if structure[0] == "prose" and units and units[-1][0] == structure:
            units[-1] = (structure, f"{units[-1][1]} {text}")
        else:
            units.append((structure, text))

    for index, raw_line in enumerate(raw_lines):
        quote_depth, quoted_line = _blockquote_prefix(raw_line)
        line = quoted_line.strip()
        if not line or table_rule.fullmatch(line):
            continue
        fenced = re.fullmatch(r"\ue100fenced:([0-9a-f]*)\ue101", line)
        if fenced:
            append(("code", quote_depth, "fenced"), fenced.group(1))
            continue
        if index in table_rows:
            append(
                ("table", quote_depth, "header" if index in table_headers else "row"),
                line,
            )
            continue
        match = list_marker.match(quoted_line)
        if match:
            indentation = len(match.group("indent").expandtabs(4))
            if indentation % 2:
                return []
            marker = match.group("marker")
            body = match.group("body")
            checkbox = re.match(r"^\[([ x])\][ \t]+(.*)$", body)
            state = None
            if checkbox:
                state = "checked" if checkbox.group(1) == "x" else "unchecked"
                body = checkbox.group(2)
            structure: tuple[object, ...]
            if marker == "-":
                structure = ("list", quote_depth, indentation // 2, "unordered", state)
            else:
                structure = (
                    "list",
                    quote_depth,
                    indentation // 2,
                    "ordered",
                    int(marker[:-1]),
                    state,
                )
            append(structure, body)
            continue
        append(("prose", quote_depth), line)
    return units


def _citation_units_contain(
    source: list[tuple[tuple[object, ...], str]],
    requirement: list[tuple[tuple[object, ...], str]],
) -> bool:
    """Match one contiguous structured quotation inside a heading region."""
    if not requirement or len(requirement) > len(source):
        return False
    if len(requirement) == 1:
        structure, text = requirement[0]
        return any(candidate == structure and text in content for candidate, content in source)
    for start in range(len(source) - len(requirement) + 1):
        window = source[start : start + len(requirement)]
        if any(left[0] != right[0] for left, right in zip(window, requirement, strict=True)):
            continue
        if not window[0][1].endswith(requirement[0][1]):
            continue
        if not window[-1][1].startswith(requirement[-1][1]):
            continue
        if all(
            left[1] == right[1] for left, right in zip(window[1:-1], requirement[1:-1], strict=True)
        ):
            return True
    return False


def _citation_flat_root_stream(value: str, *, canonical_input: bool = False) -> str | None:
    """Flatten only presentation-level prose and root list boundaries.

    Planning agents occasionally quote a root-level Markdown list as one prose
    paragraph.  That is citation-equivalent when every list item remains in
    place: unordered glyphs carry no meaning, while ordered ordinals do.  More
    strongly scoped constructs deliberately have no flat projection, so this
    fallback cannot erase checkbox state, nesting, blockquotes, tables, or
    fenced-code boundaries.
    """
    rendered: list[str] = []
    for structure, text in _citation_units(value, canonical_input=canonical_input):
        kind = structure[0]
        if kind == "prose" and structure[1] == 0:
            rendered.append(text)
            continue
        if kind != "list" or structure[1] != 0 or structure[2] != 0:
            return None
        if structure[3] == "unordered":
            if structure[4] is not None:
                return None
            rendered.append(text)
            continue
        if structure[5] is not None:
            return None
        rendered.append(f"{structure[4]}. {text}")
    stream = re.sub(r"\s+", " ", " ".join(rendered)).strip()
    return stream or None


def _citation_scoped_prose_contains(
    source: list[tuple[tuple[object, ...], str]],
    requirement: list[tuple[tuple[object, ...], str]],
) -> bool:
    """Allow an exact prose citation wholly inside one blockquote scope.

    A blockquote marker may be used as callout presentation in a specification.
    Its boundary still matters: this fallback considers one quote-scoped prose
    unit at a time, so a citation cannot flatten text across entry, exit, or
    sibling quote boundaries.
    """
    if len(requirement) != 1 or requirement[0][0] != ("prose", 0):
        return False
    wanted = requirement[0][1]
    return any(
        structure[0] == "prose" and structure[1] > 0 and wanted in content
        for structure, content in source
    )


def _citation_isolated_table_row(
    value: str, *, canonical_input: bool = False
) -> tuple[int, str] | None:
    """Return one standalone table row without erasing its semantic cells.

    A row quoted without its table header is still a useful atomic citation.  It
    cannot be classified as a table by the normal Markdown parser, however,
    because Markdown establishes table structure with the following delimiter
    row.  Recognize only one physical row here; multi-row quotations continue
    through the fully structured matcher.
    """
    lines = [
        line for line in value.replace("\r\n", "\n").replace("\r", "\n").split("\n") if line.strip()
    ]
    if len(lines) != 1:
        return None
    quote_depth, line = _blockquote_prefix(lines[0])
    encoded, _ = _citation_code_tokens(line if canonical_input else canonical_source_text(line))
    row = _canonical_table_row(encoded.strip())
    if row is None:
        return None
    return quote_depth, encoded.strip() if canonical_input else row[0]


def _citation_table_row_contains(
    source: list[tuple[tuple[object, ...], str]],
    requirement: tuple[int, str] | None,
) -> bool:
    """Match an isolated row only to the identical row of a source table."""
    if requirement is None:
        return False
    quote_depth, wanted = requirement
    return any(
        structure == ("table", quote_depth, "row") and content == wanted
        for structure, content in source
    )


def _source_requirement_present(
    requirement: str,
    section: str,
    *,
    requirement_is_canonical: bool = False,
) -> bool:
    """Match a semantic citation without weakening authoritative source hashes."""
    if requirement_is_canonical:
        requirement_regions, requirement_has_heading = [requirement], False
    else:
        requirement_regions, requirement_has_heading = _citation_markdown_regions(requirement)
    if requirement_has_heading or len(requirement_regions) != 1:
        return False
    wanted = _citation_units(requirement_regions[0], canonical_input=requirement_is_canonical)
    wanted_flat = _citation_flat_root_stream(
        requirement_regions[0], canonical_input=requirement_is_canonical
    )
    wanted_table_row = _citation_isolated_table_row(
        requirement_regions[0], canonical_input=requirement_is_canonical
    )
    source_regions, _ = _citation_markdown_regions(section)
    for region in source_regions:
        source_units = _citation_units(region)
        if _citation_units_contain(source_units, wanted):
            return True
        if _citation_table_row_contains(source_units, wanted_table_row):
            return True
        if _citation_scoped_prose_contains(source_units, wanted):
            return True
        source_flat = _citation_flat_root_stream(region)
        if wanted_flat is not None and source_flat is not None and wanted_flat in source_flat:
            return True
    return False


def _roadmap_human_view(text: str) -> str:
    if ROADMAP_VIEW_MARKER not in text:
        raise RoadmapError("roadmap is missing its generated human view marker")
    lines = text.splitlines(keepends=True)
    for entry in _markdown_heading_entries(lines):
        if entry.canonical_title == "Machine-readable roadmap data":
            return "".join(lines[: entry.line_index])
    raise RoadmapError("roadmap is missing its machine-readable data heading")


def _opening_markdown_fence(line: str) -> tuple[str, int] | None:
    match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
    if match is None:
        return None
    marker, remainder = match.groups()
    if marker[0] == "`" and "`" in remainder:
        return None
    return marker[0], len(marker)


def _closes_markdown_fence(line: str, fence: tuple[str, int]) -> bool:
    match = re.fullmatch(r" {0,3}(`+|~+)[ \t]*", line)
    if match is None:
        return False
    marker = match.group(1)
    return marker[0] == fence[0] and len(marker) >= fence[1]


def _markdown_heading_entries(
    lines: list[str],
) -> list[_MarkdownHeading]:
    """Index ATX headings outside fences with occurrence-qualified lineages."""
    entries: list[_MarkdownHeading] = []
    ancestors: list[tuple[int, tuple[tuple[int, str], ...]]] = []
    occurrences: dict[tuple[tuple[tuple[int, str], ...], str], int] = {}
    fence: tuple[str, int] | None = None
    for index, line in enumerate(lines):
        source_line = line.rstrip("\r\n")
        if fence is not None:
            if _closes_markdown_fence(source_line, fence):
                fence = None
            continue
        fence = _opening_markdown_fence(source_line)
        if fence is not None:
            continue
        heading = _atx_heading(source_line)
        if heading is None:
            continue
        level, complete_title = heading
        title = _normalize_heading(complete_title)
        while ancestors and ancestors[-1][0] >= level:
            ancestors.pop()
        parent = ancestors[-1][1] if ancestors else ()
        identity = (parent, title)
        occurrence = occurrences.get(identity, 0) + 1
        occurrences[identity] = occurrence
        lineage = (*parent, (occurrence, title))
        entries.append(
            _MarkdownHeading(
                line_index=index,
                level=level,
                displayed_title=complete_title,
                canonical_title=title,
                lineage=lineage,
            )
        )
        ancestors.append((level, lineage))
    return entries


def _canonical_anchor_for_heading(
    heading: _MarkdownHeading,
    entries: list[_MarkdownHeading],
) -> str:
    count = sum(candidate.canonical_title == heading.canonical_title for candidate in entries)
    if heading.canonical_title and count == 1:
        if not heading.canonical_title.startswith(_RESERVED_ANCHOR_PREFIX):
            return heading.canonical_title
        return LITERAL_HEADING_ANCHOR_PREFIX + quote(heading.displayed_title, safe="-._~")
    return _format_heading_lineage_anchor(heading.lineage)


def _markdown_heading_section(
    lines: list[str],
    entries: list[_MarkdownHeading],
    selected: _MarkdownHeading,
) -> str:
    end = len(lines)
    for candidate in entries:
        if candidate.line_index > selected.line_index and candidate.level <= selected.level:
            end = candidate.line_index
            break
    return "".join(lines[selected.line_index : end]).rstrip() + "\n"


def _anchor_resolution_error(source: str, anchor: str) -> RoadmapError:
    return RoadmapError(
        f"source anchor must name exactly one Markdown heading using its canonical "
        f"representation in {source}: {anchor!r}"
    )


def _resolve_markdown_section(
    text: str,
    anchor: str,
    source: str,
    *,
    requirement: str | None = None,
    requirement_is_canonical: bool = False,
) -> tuple[str, str]:
    try:
        source_anchor = validate_source_anchor(anchor)
    except ValueError as error:
        raise _anchor_resolution_error(source, anchor) from error
    lines = text.splitlines(keepends=True)
    entries = _markdown_heading_entries(lines)

    if source_anchor == DOCUMENT_ROOT_ANCHOR:
        end = entries[0].line_index if entries else len(lines)
        return "".join(lines[:end]).rstrip() + "\n", DOCUMENT_ROOT_ANCHOR

    lineage = _parse_heading_lineage_anchor(source_anchor)
    if lineage is not None:
        matches = [entry for entry in entries if entry.lineage == lineage]
        if len(matches) != 1 or _canonical_anchor_for_heading(matches[0], entries) != anchor:
            raise _anchor_resolution_error(source, anchor)
        return _markdown_heading_section(lines, entries, matches[0]), anchor

    literal_title = _parse_literal_heading_anchor(source_anchor)
    if literal_title is not None:
        matches = [entry for entry in entries if entry.displayed_title == literal_title]
        if len(matches) != 1 or _canonical_anchor_for_heading(matches[0], entries) != anchor:
            raise _anchor_resolution_error(source, anchor)
        return _markdown_heading_section(lines, entries, matches[0]), anchor

    if requirement is not None and source_anchor in {"C", "F"}:
        exact = [
            entry
            for entry in entries
            if _canonical_anchor_for_heading(entry, entries) == source_anchor
        ]
        if len(exact) == 1:
            exact_section = _markdown_heading_section(lines, entries, exact[0])
            if _source_requirement_present(
                requirement,
                exact_section,
                requirement_is_canonical=requirement_is_canonical,
            ):
                return exact_section, source_anchor
        candidates = [
            entry
            for entry in entries
            if entry.canonical_title in {source_anchor, source_anchor + "#"}
        ]
        supported = [
            entry
            for entry in candidates
            if _source_requirement_present(
                requirement,
                _markdown_heading_section(lines, entries, entry),
                requirement_is_canonical=requirement_is_canonical,
            )
        ]
        if len(supported) != 1:
            raise _anchor_resolution_error(source, anchor)
        selected = supported[0]
        resolved = _canonical_anchor_for_heading(selected, entries)
        return _markdown_heading_section(lines, entries, selected), resolved

    # Ordinary planner anchors are already canonical semantic titles.  Resolve
    # that identity before considering legacy Markdown presentation so a valid
    # title such as ``# Rules`` cannot be retargeted to ``Rules``.
    direct = [
        entry for entry in entries if _canonical_anchor_for_heading(entry, entries) == source_anchor
    ]
    if len(direct) == 1:
        resolved = _canonical_anchor_for_heading(direct[0], entries)
        return _markdown_heading_section(lines, entries, direct[0]), resolved
    if any(entry.canonical_title == source_anchor for entry in entries):
        raise _anchor_resolution_error(source, anchor)

    legacy_anchor = _canonical_legacy_anchor(source_anchor)
    if not legacy_anchor or legacy_anchor.startswith(_RESERVED_ANCHOR_PREFIX):
        raise _anchor_resolution_error(source, anchor)
    if requirement is not None and legacy_anchor in {"C", "F"}:
        candidates = [
            entry
            for entry in entries
            if entry.canonical_title in {legacy_anchor, legacy_anchor + "#"}
        ]
        supported = [
            entry
            for entry in candidates
            if _source_requirement_present(
                requirement,
                _markdown_heading_section(lines, entries, entry),
                requirement_is_canonical=requirement_is_canonical,
            )
        ]
        if len(supported) != 1:
            raise _anchor_resolution_error(source, anchor)
        selected = supported[0]
        resolved = _canonical_anchor_for_heading(selected, entries)
        return _markdown_heading_section(lines, entries, selected), resolved
    legacy = [
        entry for entry in entries if _canonical_anchor_for_heading(entry, entries) == legacy_anchor
    ]
    if len(legacy) != 1:
        raise _anchor_resolution_error(source, anchor)
    resolved = _canonical_anchor_for_heading(legacy[0], entries)
    return _markdown_heading_section(lines, entries, legacy[0]), resolved


def markdown_section(text: str, anchor: str, source: str) -> str:
    section, _ = _resolve_markdown_section(text, anchor, source)
    return section


def source_anchor_present(text: str, anchor: str, source: str) -> bool:
    """Check a contract anchor while safely migrating legacy C#/F# title aliases."""
    source_anchor = validate_source_anchor(anchor)
    lines = text.splitlines(keepends=True)
    entries = _markdown_heading_entries(lines)
    if source_anchor.startswith(_RESERVED_ANCHOR_PREFIX):
        section, resolved_anchor = _resolve_markdown_section(text, source_anchor, source)
        if resolved_anchor == DOCUMENT_ROOT_ANCHOR:
            return bool(canonical_source_text(section))
        selected = next(
            entry
            for entry in entries
            if _canonical_anchor_for_heading(entry, entries) == resolved_anchor
        )
        return bool(selected.canonical_title or canonical_source_text(section))
    if source_anchor in {"C", "F"}:
        candidates = [
            entry
            for entry in entries
            if entry.canonical_title in {source_anchor, source_anchor + "#"}
        ]
        if len(candidates) > 1:
            raise _anchor_resolution_error(source, anchor)
        if candidates:
            return True
    direct = [
        entry for entry in entries if _canonical_anchor_for_heading(entry, entries) == source_anchor
    ]
    if direct:
        return True
    if any(entry.canonical_title == source_anchor for entry in entries):
        raise _anchor_resolution_error(source, anchor)
    legacy_anchor = _canonical_legacy_anchor(source_anchor)
    if not legacy_anchor or legacy_anchor.startswith(_RESERVED_ANCHOR_PREFIX):
        raise _anchor_resolution_error(source, anchor)
    if legacy_anchor in {"C", "F"}:
        candidates = [
            entry
            for entry in entries
            if entry.canonical_title in {legacy_anchor, legacy_anchor + "#"}
        ]
        if len(candidates) > 1:
            raise _anchor_resolution_error(source, anchor)
        if candidates:
            return True
    exact_or_legacy_titles = {source_anchor, legacy_anchor}
    heading_candidates = [
        entry for entry in entries if entry.canonical_title in exact_or_legacy_titles
    ]
    if heading_candidates:
        markdown_section(text, source_anchor, source)
        return True
    # Historical contracts also used stable prose identifiers rather than
    # Markdown headings. Preserve that fallback only when the value cannot name
    # any heading under either the canonical or legacy projection.
    return source_anchor in text


def _worktree_reader(repository: Path) -> Callable[[str], bytes]:
    root = repository.resolve()

    def read(path: str) -> bytes:
        candidate = (root / path).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise RoadmapError(f"roadmap source is missing or escaped the repository: {path}")
        return candidate.read_bytes()

    return read


def _git_reader(repository: Path, commit: str) -> Callable[[str], bytes]:
    def read(path: str) -> bytes:
        result = subprocess.run(
            ["git", "-C", str(repository), "show", f"{commit}:{path}"],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RoadmapError(
                result.stderr.decode(errors="replace").strip()
                or f"roadmap source is absent from {commit}: {path}"
            )
        return result.stdout

    return read


def _source_closure(
    read: Callable[[str], bytes],
    roadmap: dict[str, Any],
    references: Iterable[dict[str, str]],
) -> list[dict[str, str]]:
    """Validate and bind every distinct source reference in one roadmap scope."""
    resolved: dict[tuple[str, str, str], dict[str, str]] = {}
    texts: dict[str, str] = {}
    specification_root = roadmap["specification_root"].rstrip("/")
    for reference in sorted(
        references, key=lambda item: (item["path"], item["anchor"], item["requirement"])
    ):
        if not (
            reference["path"] == specification_root
            or reference["path"].startswith(specification_root + "/")
        ):
            raise RoadmapError(
                f"stage source must live under {roadmap['specification_root']}: {reference['path']}"
            )
        if reference["path"] not in texts:
            raw = read(reference["path"])
            try:
                texts[reference["path"]] = raw.decode("utf-8")
            except UnicodeDecodeError as error:
                raise RoadmapError(
                    f"source specification is not UTF-8: {reference['path']}"
                ) from error
        section, resolved_anchor = _resolve_markdown_section(
            texts[reference["path"]],
            reference["anchor"],
            reference["path"],
            requirement=reference["requirement"],
        )
        if not _source_requirement_present(reference["requirement"], section):
            raise RoadmapError(
                f"source requirement is absent from section {reference['anchor']!r} in "
                f"{reference['path']}"
            )
        canonical_requirement = canonical_source_text(reference["requirement"])
        identity = (reference["path"], resolved_anchor, canonical_requirement)
        resolved[identity] = {
            "path": reference["path"],
            "anchor": resolved_anchor,
            "requirement": canonical_requirement,
            "section_sha256": digest_bytes(canonical_source_text(section).encode("utf-8")),
        }
    return [resolved[identity] for identity in sorted(resolved)]


def _roadmap_qualification(read: Callable[[str], bytes], roadmap_path: str) -> dict[str, Any]:
    """Qualify the complete roadmap, including unreferenced global invariants."""
    raw_roadmap = read(roadmap_path)
    try:
        roadmap_text = raw_roadmap.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RoadmapError("roadmap is not UTF-8") from error
    roadmap = parse_roadmap(roadmap_text, roadmap_path)
    _validate_verification_policy(roadmap)
    expected_roadmap = render_roadmap_document(roadmap_text, roadmap_path)
    if canonical_source_text(_roadmap_human_view(roadmap_text)) != canonical_source_text(
        _roadmap_human_view(expected_roadmap)
    ):
        raise RoadmapError(
            "roadmap human view changes the meaning of its machine data; regenerate it with Oxide"
        )
    references = [
        *(source for stage in roadmap["stages"] for source in stage["source_specifications"]),
        *(source for invariant in roadmap["global_invariants"] for source in invariant["sources"]),
    ]
    closure = _source_closure(read, roadmap, references)
    return {
        "roadmap": roadmap,
        "roadmap_path": roadmap_path,
        "roadmap_sha256": digest_bytes(raw_roadmap),
        "semantic_closure": closure,
        "semantic_closure_sha256": digest_bytes(canonical_bytes(closure)),
    }


def _stage_binding(
    read: Callable[[str], bytes], roadmap_path: str, stage_id: str
) -> dict[str, Any]:
    raw_roadmap = read(roadmap_path)
    try:
        roadmap_text = raw_roadmap.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RoadmapError("roadmap is not UTF-8") from error
    roadmap = parse_roadmap(roadmap_text, roadmap_path)
    _validate_verification_policy(roadmap)
    expected_roadmap = render_roadmap_document(roadmap_text, roadmap_path)
    if canonical_source_text(_roadmap_human_view(roadmap_text)) != canonical_source_text(
        _roadmap_human_view(expected_roadmap)
    ):
        raise RoadmapError(
            "roadmap human view changes the meaning of its machine data; regenerate it with Oxide"
        )
    stage = next((item for item in roadmap["stages"] if item["id"] == stage_id), None)
    if stage is None:
        raise RoadmapError(f"roadmap contains no stage {stage_id!r}")
    by_invariant = {item["id"]: item for item in roadmap["global_invariants"]}
    invariants = [by_invariant[identifier] for identifier in stage["applicable_global_invariants"]]
    references = [
        *stage["source_specifications"],
        *(source for invariant in invariants for source in invariant["sources"]),
    ]
    closure = _source_closure(read, roadmap, references)
    return {
        "roadmap_path": roadmap_path,
        "roadmap_sha256": digest_bytes(raw_roadmap),
        "stage_id": stage_id,
        "stage": stage,
        "stage_sha256": digest_bytes(canonical_bytes(stage)),
        "global_invariants": invariants,
        "global_invariants_sha256": digest_bytes(canonical_bytes(invariants)),
        "semantic_closure": closure,
        "semantic_closure_sha256": digest_bytes(canonical_bytes(closure)),
        "specification_root": roadmap["specification_root"],
    }


def stage_binding(
    repository: Path,
    roadmap_path: str,
    stage_id: str,
    *,
    commit: str | None = None,
) -> dict[str, Any]:
    read = _git_reader(repository, commit) if commit else _worktree_reader(repository)
    return _stage_binding(read, roadmap_path, stage_id)


def stage_set_binding(
    repository: Path,
    roadmap_path: str,
    stage_ids: Iterable[str],
    *,
    commit: str | None = None,
) -> dict[str, Any]:
    """Bind an ordered set of roadmap phases and its deduplicated semantic closure."""
    requested = list(stage_ids)
    if not requested or len(requested) != len(set(requested)):
        raise RoadmapError("contract generation must select one or more unique phases")
    roadmap_raw = (
        _git_reader(repository, commit)(roadmap_path)
        if commit
        else _worktree_reader(repository)(roadmap_path)
    )
    roadmap = parse_roadmap(roadmap_raw.decode("utf-8"), roadmap_path)
    order = [stage["id"] for stage in roadmap["stages"]]
    unknown = sorted(set(requested) - set(order))
    if unknown:
        raise RoadmapError("roadmap contains no phases: " + ", ".join(unknown))
    selected = [identifier for identifier in order if identifier in set(requested)]
    if selected != requested:
        raise RoadmapError("selected phases must follow roadmap order")
    bindings = [stage_binding(repository, roadmap_path, item, commit=commit) for item in selected]
    selected_set = set(selected)
    for binding in bindings:
        missing = [
            dependency
            for dependency in binding["stage"]["dependencies"]
            if dependency not in selected_set
        ]
        if missing:
            raise RoadmapError(
                f"phase {binding['stage_id']!r} requires selected dependencies: "
                + ", ".join(missing)
            )
    closure: dict[tuple[str, str, str], dict[str, str]] = {}
    invariants: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        for item in binding["semantic_closure"]:
            closure[(item["path"], item["anchor"], item["requirement"])] = item
        for invariant in binding["global_invariants"]:
            invariants[invariant["id"]] = invariant
    invariant_ids = sorted(invariants, key=_invariant_order)
    return {
        "roadmap_path": roadmap_path,
        "roadmap_sha256": digest_bytes(roadmap_raw),
        "stage_ids": selected,
        "stages": [binding["stage"] for binding in bindings],
        "stage_set_sha256": digest_bytes(
            canonical_bytes([binding["stage"] for binding in bindings])
        ),
        "global_invariants": [invariants[key] for key in invariant_ids],
        "global_invariants_sha256": digest_bytes(
            canonical_bytes([invariants[key] for key in invariant_ids])
        ),
        "semantic_closure": [closure[key] for key in sorted(closure)],
        "semantic_closure_sha256": digest_bytes(
            canonical_bytes([closure[key] for key in sorted(closure)])
        ),
        "specification_root": bindings[0]["specification_root"],
    }


def proposed_stage_binding(
    repository: Path,
    roadmap_path: str,
    roadmap_content: str,
    stage_id: str,
    replacements: dict[str, str],
) -> dict[str, Any]:
    """Bind an in-memory proposal without writing it into the target worktree."""
    current = _worktree_reader(repository)
    encoded = {path: content.encode("utf-8") for path, content in replacements.items()}
    encoded[roadmap_path] = roadmap_content.encode("utf-8")

    def read(path: str) -> bytes:
        return encoded[path] if path in encoded else current(path)

    return _stage_binding(read, roadmap_path, stage_id)


def proposed_roadmap_qualification(
    repository: Path,
    roadmap_path: str,
    roadmap_content: str,
    replacements: dict[str, str],
) -> dict[str, Any]:
    """Qualify every source and universal-policy obligation before review."""
    current = _worktree_reader(repository)
    encoded = {path: content.encode("utf-8") for path, content in replacements.items()}
    encoded[roadmap_path] = roadmap_content.encode("utf-8")

    def read(path: str) -> bytes:
        return encoded[path] if path in encoded else current(path)

    return _roadmap_qualification(read, roadmap_path)


def proposed_stage_set_binding(
    repository: Path,
    roadmap_path: str,
    roadmap_content: str,
    stage_ids: Iterable[str],
    replacements: dict[str, str],
) -> dict[str, Any]:
    """Bind a multi-phase in-memory proposal before any approved file is written."""
    current = _worktree_reader(repository)
    encoded = {path: content.encode("utf-8") for path, content in replacements.items()}
    encoded[roadmap_path] = roadmap_content.encode("utf-8")

    def read(path: str) -> bytes:
        return encoded[path] if path in encoded else current(path)

    requested = list(stage_ids)
    roadmap_raw = read(roadmap_path)
    roadmap = parse_roadmap(roadmap_raw.decode("utf-8"), roadmap_path)
    order = [phase["id"] for phase in roadmap["stages"]]
    selected = [identifier for identifier in order if identifier in set(requested)]
    if selected != requested:
        raise RoadmapError("selected phases must exist once and follow roadmap order")
    bindings = [_stage_binding(read, roadmap_path, identifier) for identifier in selected]
    selected_set = set(selected)
    for binding in bindings:
        missing = [
            dependency
            for dependency in binding["stage"]["dependencies"]
            if dependency not in selected_set
        ]
        if missing:
            raise RoadmapError(
                f"phase {binding['stage_id']!r} requires selected dependencies: "
                + ", ".join(missing)
            )
    closure: dict[tuple[str, str, str], dict[str, str]] = {}
    invariants: dict[str, dict[str, Any]] = {}
    for binding in bindings:
        for item in binding["semantic_closure"]:
            closure[(item["path"], item["anchor"], item["requirement"])] = item
        for invariant in binding["global_invariants"]:
            invariants[invariant["id"]] = invariant
    invariant_ids = sorted(invariants, key=_invariant_order)
    return {
        "roadmap_path": roadmap_path,
        "roadmap_sha256": digest_bytes(roadmap_raw),
        "stage_ids": selected,
        "stages": [binding["stage"] for binding in bindings],
        "stage_set_sha256": digest_bytes(
            canonical_bytes([binding["stage"] for binding in bindings])
        ),
        "global_invariants": [invariants[key] for key in invariant_ids],
        "global_invariants_sha256": digest_bytes(
            canonical_bytes([invariants[key] for key in invariant_ids])
        ),
        "semantic_closure": [closure[key] for key in sorted(closure)],
        "semantic_closure_sha256": digest_bytes(
            canonical_bytes([closure[key] for key in sorted(closure)])
        ),
        "specification_root": bindings[0]["specification_root"],
    }


def specification_corpus(repository: Path, directory: str) -> list[dict[str, str]]:
    root = (repository / directory).resolve()
    if not root.is_dir() or not root.is_relative_to(repository.resolve()):
        raise RoadmapError("specification directory must be inside the target repository")
    files = sorted(path for path in root.rglob("*.md") if path.is_file())
    if not files:
        raise RoadmapError("specification directory contains no Markdown specifications")
    return [
        {
            "path": path.relative_to(repository).as_posix(),
            "sha256": digest_bytes(path.read_bytes()),
        }
        for path in files
    ]
