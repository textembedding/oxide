"""Standardized, source-traced roadmaps and scope-aware planning approval."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import subprocess
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .verification_policy import verification_policy_digest


class RoadmapError(RuntimeError):
    pass


ROADMAP_MARKER = "<!-- oxide-roadmap-schema:1 -->"
ROADMAP_VIEW_MARKER = "<!-- oxide-roadmap-view:1 -->"
ROADMAP_APPROVAL_PATH = "verification/roadmap-approval.json"
ROADMAP_SCHEMA = 1
ROADMAP_APPROVAL_SCHEMA = "OxideRoadmapApprovalV2"
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40,64}$")
_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$")


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


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


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
        normalized = (path, anchor.strip(), requirement.strip())
        if normalized in seen:
            raise RoadmapError(f"{field} contains duplicate source requirements")
        seen.add(normalized)
        result.append(
            {"path": normalized[0], "anchor": normalized[1], "requirement": normalized[2]}
        )
    return result


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
        raise RoadmapError("roadmap schema must be a TOML table")
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
        applicable = _strings(
            raw.get("applicable_global_invariants"),
            f"stage {identifier}.applicable_global_invariants",
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
                "dependencies": _strings(
                    raw.get("dependencies"), f"stage {identifier}.dependencies"
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
    remaining = {stage["id"]: set(stage["dependencies"]) for stage in stages}
    while remaining:
        ready = {
            identifier for identifier, deps in remaining.items() if not deps & remaining.keys()
        }
        if not ready:
            raise RoadmapError("roadmap stage dependency graph contains a cycle")
        for identifier in ready:
            remaining.pop(identifier)
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
    if not changes:
        raise RoadmapError("maintenance proposal makes no change to the selected phases")

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


def _heading_slug(value: str) -> str:
    normalized = _normalize_heading(value).lower()
    normalized = re.sub(r"[^\w\s-]", "", normalized)
    return re.sub(r"[\s-]+", "-", normalized).strip("-")


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
        slug = _heading_slug(anchor)
        if slug:
            target += "#" + quote(slug, safe="-_")
        label = f"{Path(path).name} — {_normalize_heading(anchor)}"
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


def render_roadmap_document(text: str, source: str | Path = "ROADMAP.md") -> str:
    """Create the canonical human view around the authoritative TOML schema.

    Agent-authored prose outside the schema block is intentionally discarded. This
    leaves one semantic representation: the validated TOML. The readable Markdown
    is a deterministic projection and therefore cannot drift independently.
    """
    roadmap = parse_roadmap(text, source)
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


def load_roadmap(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        return parse_roadmap(source.read_text(encoding="utf-8"), source)
    except (OSError, UnicodeDecodeError) as error:
        raise RoadmapError(f"cannot read roadmap {source}: {error}") from error


def _normalize_heading(value: str) -> str:
    return re.sub(r"[ \t]+", " ", value.strip().lstrip("#").strip().rstrip("#").strip())


def markdown_section(text: str, anchor: str, source: str) -> str:
    wanted = _normalize_heading(anchor)
    lines = text.splitlines(keepends=True)
    matches: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line.rstrip("\r\n"))
        if match and _normalize_heading(match.group(2)) == wanted:
            matches.append((index, len(match.group(1))))
    if len(matches) != 1:
        raise RoadmapError(
            f"source anchor must name exactly one Markdown heading in {source}: {anchor!r}"
        )
    start, level = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        match = _HEADING.match(lines[index].rstrip("\r\n"))
        if match and len(match.group(1)) <= level:
            end = index
            break
    return "".join(lines[start:end]).rstrip() + "\n"


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


def _stage_binding(
    read: Callable[[str], bytes], roadmap_path: str, stage_id: str
) -> dict[str, Any]:
    raw_roadmap = read(roadmap_path)
    try:
        roadmap_text = raw_roadmap.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RoadmapError("roadmap is not UTF-8") from error
    roadmap = parse_roadmap(roadmap_text, roadmap_path)
    if roadmap_text != render_roadmap_document(roadmap_text, roadmap_path):
        raise RoadmapError(
            "roadmap human view does not match its machine data; regenerate it with Oxide"
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
    unique: dict[tuple[str, str, str], dict[str, str]] = {}
    for reference in references:
        unique[(reference["path"], reference["anchor"], reference["requirement"])] = reference
    closure: list[dict[str, str]] = []
    specification_root = roadmap["specification_root"].rstrip("/")
    for reference in sorted(
        unique.values(), key=lambda item: (item["path"], item["anchor"], item["requirement"])
    ):
        if not (
            reference["path"] == specification_root
            or reference["path"].startswith(specification_root + "/")
        ):
            raise RoadmapError(
                f"stage source must live under {roadmap['specification_root']}: {reference['path']}"
            )
        raw = read(reference["path"])
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise RoadmapError(f"source specification is not UTF-8: {reference['path']}") from error
        section = markdown_section(text, reference["anchor"], reference["path"])
        if reference["requirement"] not in section:
            raise RoadmapError(
                f"source requirement is absent from section {reference['anchor']!r} in "
                f"{reference['path']}"
            )
        closure.append(
            {
                **reference,
                "section_sha256": digest_bytes(section.encode("utf-8")),
            }
        )
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


def planning_policy_digest() -> str:
    return digest_bytes(
        canonical_bytes(
            {
                "roadmap_module": digest_bytes(Path(__file__).read_bytes()),
                "roadmap_schema": ROADMAP_SCHEMA,
                "approval_schema": ROADMAP_APPROVAL_SCHEMA,
                "verification_policy_sha256": verification_policy_digest(),
            }
        )
    )


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


def _repository_head(repository: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unborn"


def _valid_identity(value: object) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"name", "email"}
        and all(isinstance(item, str) and item.strip() for item in value.values())
        and "@" in value["email"]
    )


def build_roadmap_approval(
    repository: Path,
    roadmap_path: str,
    *,
    specification_directory: str,
    stage_ids: Iterable[str],
    agent: dict[str, Any],
    user_identity: dict[str, str],
    prior: dict[str, Any] | None = None,
    invalidated_stage_ids: Iterable[str] = (),
) -> dict[str, Any]:
    if (
        not isinstance(agent, dict)
        or set(agent)
        != {
            "identity",
            "complete_specification_corpus",
            "faithful_to_specifications",
            "unresolved",
        }
        or not isinstance(agent["identity"], str)
        or not agent["identity"].strip()
        or agent["complete_specification_corpus"] is not True
        or agent["faithful_to_specifications"] is not True
        or agent["unresolved"] != []
    ):
        raise RoadmapError("planning agent has not provided a complete roadmap attestation")
    if not _valid_identity(user_identity):
        raise RoadmapError("approving user Git identity is malformed")
    path = _relative_path(roadmap_path, "roadmap approval path")
    roadmap = load_roadmap(repository / path)
    if roadmap["status"] != "ready":
        raise RoadmapError("a draft roadmap cannot be approved")
    requested = list(stage_ids)
    if not requested or len(requested) != len(set(requested)):
        raise RoadmapError("roadmap approval must identify one or more unique stages")
    invalidated = set(invalidated_stage_ids)
    approvals: dict[str, dict[str, Any]] = {}
    if isinstance(prior, dict) and prior.get("schema") == ROADMAP_APPROVAL_SCHEMA:
        for item in prior.get("stage_approvals", []):
            if not isinstance(item, dict) or not isinstance(item.get("stage_id"), str):
                continue
            if item["stage_id"] in invalidated:
                continue
            try:
                current = stage_binding(repository, path, item["stage_id"])
            except RoadmapError:
                continue
            if all(
                item.get(field) == current[field]
                for field in (
                    "stage_sha256",
                    "global_invariants_sha256",
                    "semantic_closure_sha256",
                )
            ):
                approvals[item["stage_id"]] = item
    for stage_id in requested:
        binding = stage_binding(repository, path, stage_id)
        approvals[stage_id] = {
            "stage_id": stage_id,
            "stage_sha256": binding["stage_sha256"],
            "global_invariants_sha256": binding["global_invariants_sha256"],
            "semantic_closure": binding["semantic_closure"],
            "semantic_closure_sha256": binding["semantic_closure_sha256"],
        }
    raw = (repository / path).read_bytes()
    return {
        "schema": ROADMAP_APPROVAL_SCHEMA,
        "status": "approved",
        "roadmap_path": path,
        "roadmap_sha256_at_approval": digest_bytes(raw),
        "repository_revision": _repository_head(repository),
        "specification_directory": _relative_path(
            specification_directory, "roadmap specification directory"
        ),
        "specification_corpus_at_approval": specification_corpus(
            repository, specification_directory
        ),
        "planning_policy_sha256": planning_policy_digest(),
        "verification_policy_sha256": verification_policy_digest(),
        "agent": agent,
        "user": {
            "name": user_identity["name"].strip(),
            "email": user_identity["email"].strip(),
            "approved": True,
        },
        "stage_approvals": [approvals[key] for key in sorted(approvals)],
    }


def write_roadmap_approval(
    repository: Path,
    roadmap_path: str,
    *,
    specification_directory: str,
    stage_ids: Iterable[str],
    agent: dict[str, Any],
    user_identity: dict[str, str],
    destination: str = ROADMAP_APPROVAL_PATH,
    invalidated_stage_ids: Iterable[str] = (),
) -> Path:
    path = (repository / destination).resolve()
    if not path.is_relative_to(repository.resolve()):
        raise RoadmapError("roadmap approval receipt escaped the repository")
    prior: dict[str, Any] | None = None
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            prior = loaded if isinstance(loaded, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            prior = None
    receipt = build_roadmap_approval(
        repository,
        roadmap_path,
        specification_directory=specification_directory,
        stage_ids=stage_ids,
        agent=agent,
        user_identity=user_identity,
        prior=prior,
        invalidated_stage_ids=invalidated_stage_ids,
    )
    _atomic_json(path, receipt)
    return path


def _load_json_bytes(raw: bytes, description: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RoadmapError(f"{description} is unreadable") from error
    if not isinstance(value, dict):
        raise RoadmapError(f"{description} must be an object")
    return value


def validate_roadmap_approval(
    repository: Path,
    roadmap_path: str,
    stage_id: str,
    *,
    commit: str | None = None,
    receipt_path: str = ROADMAP_APPROVAL_PATH,
) -> dict[str, Any]:
    read = _git_reader(repository, commit) if commit else _worktree_reader(repository)
    try:
        raw = read(receipt_path)
    except RoadmapError as error:
        raise RoadmapError(
            "selected roadmap stage has no user-approved planning receipt"
        ) from error
    receipt = _load_json_bytes(raw, "roadmap approval receipt")
    required = {
        "schema",
        "status",
        "roadmap_path",
        "roadmap_sha256_at_approval",
        "repository_revision",
        "specification_directory",
        "specification_corpus_at_approval",
        "planning_policy_sha256",
        "verification_policy_sha256",
        "agent",
        "user",
        "stage_approvals",
    }
    if set(receipt) != required or receipt.get("schema") != ROADMAP_APPROVAL_SCHEMA:
        raise RoadmapError("roadmap approval receipt has the wrong schema closure")
    if receipt.get("status") != "approved" or receipt.get("roadmap_path") != roadmap_path:
        raise RoadmapError("roadmap approval receipt does not approve this roadmap")
    if (
        _DIGEST.fullmatch(str(receipt.get("roadmap_sha256_at_approval", ""))) is None
        or _COMMIT.fullmatch(str(receipt.get("repository_revision", ""))) is None
        or not isinstance(receipt.get("specification_directory"), str)
        or not isinstance(receipt.get("specification_corpus_at_approval"), list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"path", "sha256"}
            or not isinstance(item["path"], str)
            or _DIGEST.fullmatch(str(item["sha256"])) is None
            for item in receipt["specification_corpus_at_approval"]
        )
    ):
        raise RoadmapError("roadmap approval provenance is malformed")
    if receipt.get("planning_policy_sha256") != planning_policy_digest():
        raise RoadmapError("roadmap planning policy changed; rerun the planning session")
    if receipt.get("verification_policy_sha256") != verification_policy_digest():
        raise RoadmapError("verification policy changed; rerun the planning session")
    agent, user = receipt.get("agent"), receipt.get("user")
    if (
        not isinstance(agent, dict)
        or set(agent)
        != {
            "identity",
            "complete_specification_corpus",
            "faithful_to_specifications",
            "unresolved",
        }
        or not isinstance(agent.get("identity"), str)
        or not agent["identity"].strip()
        or agent.get("complete_specification_corpus") is not True
        or agent.get("faithful_to_specifications") is not True
        or agent.get("unresolved") != []
    ):
        raise RoadmapError("planning agent attestation is incomplete")
    identity = (
        {"name": user.get("name"), "email": user.get("email")} if isinstance(user, dict) else None
    )
    if not _valid_identity(identity):
        raise RoadmapError("roadmap approval user identity is malformed")
    if (
        not isinstance(user, dict)
        or set(user) != {"name", "email", "approved"}
        or user.get("approved") is not True
    ):
        raise RoadmapError("roadmap lacks explicit user approval")
    approvals = receipt.get("stage_approvals")
    if (
        not isinstance(approvals, list)
        or not approvals
        or len(
            {
                item.get("stage_id")
                for item in approvals
                if isinstance(item, dict) and isinstance(item.get("stage_id"), str)
            }
        )
        != len(approvals)
    ):
        raise RoadmapError("roadmap stage approvals are malformed or duplicate")
    binding = stage_binding(repository, roadmap_path, stage_id, commit=commit)
    matches = [
        item for item in approvals if isinstance(item, dict) and item.get("stage_id") == stage_id
    ]
    if len(matches) != 1:
        raise RoadmapError(f"roadmap stage {stage_id!r} has not been explicitly approved")
    approved = matches[0]
    expected_fields = {
        "stage_id",
        "stage_sha256",
        "global_invariants_sha256",
        "semantic_closure",
        "semantic_closure_sha256",
    }
    if set(approved) != expected_fields:
        raise RoadmapError("roadmap stage approval is malformed")
    for field in ("stage_sha256", "global_invariants_sha256", "semantic_closure_sha256"):
        if approved.get(field) != binding[field]:
            raise RoadmapError(
                "selected roadmap stage, applicable invariant, or cited requirement changed; "
                "rerun planning or contract generation"
            )
    if approved.get("semantic_closure") != binding["semantic_closure"]:
        raise RoadmapError("selected roadmap semantic closure differs from its approval")
    return {
        "schema": ROADMAP_APPROVAL_SCHEMA,
        "receipt_path": receipt_path,
        "receipt_sha256": digest_bytes(raw),
        "stage_id": stage_id,
        "stage_sha256": binding["stage_sha256"],
        "global_invariants_sha256": binding["global_invariants_sha256"],
        "semantic_closure_sha256": binding["semantic_closure_sha256"],
        "planning_policy_sha256": receipt["planning_policy_sha256"],
        "verification_policy_sha256": receipt["verification_policy_sha256"],
        "agent_identity": str(agent.get("identity", "")),
        "approved_by": {"name": user["name"], "email": user["email"]},
        "binding": binding,
    }
