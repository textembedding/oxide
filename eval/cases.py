"""Load requirement-level planning evaluations from TOML fixtures."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any


class CaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class RequirementOracle:
    identifier: str
    path: str
    anchor: str
    text: str
    readiness: tuple[str, ...]


@dataclass(frozen=True)
class DependencyOracle:
    before: str
    after: str


@dataclass(frozen=True)
class Scenario:
    identifier: str
    directory: Path
    specification_directory: str
    expected_approval: bool
    minimum_unresolved: int
    required_signal_groups: tuple[tuple[str, ...], ...]
    forbidden_terms: tuple[str, ...]
    requirements: tuple[RequirementOracle, ...]
    dependencies: tuple[DependencyOracle, ...]
    model_free_output: Path
    fixture_unresolved: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationCase:
    identifier: str
    title: str
    relation: str
    affected_requirements: tuple[str, ...]
    base: Scenario
    variant: Scenario


def _strings(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise CaseError(f"{field} must be a list of nonempty strings")
    return tuple(value)


def _scenario(case_directory: Path, value: object) -> Scenario:
    if not isinstance(value, dict) or set(value) != {"id", "directory"}:
        raise CaseError("each scenario must contain exactly id and directory")
    identifier, relative = value["id"], value["directory"]
    if not isinstance(identifier, str) or not identifier:
        raise CaseError("scenario.id must be nonempty")
    if not isinstance(relative, str) or not relative:
        raise CaseError("scenario.directory must be nonempty")
    directory = (case_directory / relative).resolve()
    if not directory.is_relative_to(case_directory.resolve()) or not directory.is_dir():
        raise CaseError(f"scenario directory is absent or escaped: {relative}")
    rubric_path = directory / "rubric.toml"
    try:
        rubric = tomllib.loads(rubric_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CaseError(f"cannot load {rubric_path}: {error}") from error
    allowed = {
        "expected_approval",
        "minimum_unresolved",
        "required_signal_groups",
        "forbidden_terms",
        "fixture_unresolved",
        "requirements",
        "dependencies",
    }
    if set(rubric) != allowed:
        raise CaseError(
            f"{rubric_path} has unsupported or missing fields: {sorted(set(rubric) ^ allowed)}"
        )
    expected = rubric["expected_approval"]
    minimum = rubric["minimum_unresolved"]
    if not isinstance(expected, bool) or not isinstance(minimum, int) or minimum < 0:
        raise CaseError(f"{rubric_path} has invalid approval expectations")
    groups_raw = rubric["required_signal_groups"]
    if not isinstance(groups_raw, list):
        raise CaseError(f"{rubric_path}.required_signal_groups must be a list")
    groups = tuple(_strings(group, "required signal group") for group in groups_raw)
    requirements_raw = rubric["requirements"]
    if not isinstance(requirements_raw, list) or not requirements_raw:
        raise CaseError(f"{rubric_path}.requirements must not be empty")
    repository = case_directory.parents[2]
    specification_root = specs = directory / "specs"
    if not specs.is_dir() or not list(specs.rglob("*.md")):
        raise CaseError(f"{directory} has no specification corpus")
    specification_prefix = specs.relative_to(repository).as_posix()
    requirements: list[RequirementOracle] = []
    for item in requirements_raw:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "path",
            "anchor",
            "text",
            "readiness",
        }:
            raise CaseError(f"{rubric_path} has a malformed requirement")
        source_path = item["path"]
        if not isinstance(source_path, str) or not source_path:
            raise CaseError(f"{rubric_path} has an invalid requirement source path")
        relative_source = PurePosixPath(source_path)
        if relative_source.is_absolute() or ".." in relative_source.parts:
            raise CaseError(f"{rubric_path} has an escaped requirement source path")
        source_file = specification_root.joinpath(*relative_source.parts)
        if not source_file.is_file():
            raise CaseError(f"{rubric_path} requirement source is absent: {source_path}")
        readiness = _strings(item["readiness"], f"requirement {item.get('id')}.readiness")
        if not readiness or not set(readiness) <= {"planned", "ready", "deferred", "blocked"}:
            raise CaseError(f"{rubric_path} has invalid requirement readiness")
        requirements.append(
            RequirementOracle(
                identifier=str(item["id"]),
                path=f"{specification_prefix}/{relative_source.as_posix()}",
                anchor=str(item["anchor"]),
                text=str(item["text"]),
                readiness=readiness,
            )
        )
    identifiers = [item.identifier for item in requirements]
    if any(not item for item in identifiers) or len(set(identifiers)) != len(identifiers):
        raise CaseError(f"{rubric_path} has empty or duplicate requirement IDs")
    dependencies_raw = rubric["dependencies"]
    if not isinstance(dependencies_raw, list):
        raise CaseError(f"{rubric_path}.dependencies must be a list")
    dependencies: list[DependencyOracle] = []
    for item in dependencies_raw:
        if not isinstance(item, dict) or set(item) != {"before", "after"}:
            raise CaseError(f"{rubric_path} has a malformed dependency")
        dependency = DependencyOracle(str(item["before"]), str(item["after"]))
        if dependency.before not in identifiers or dependency.after not in identifiers:
            raise CaseError(f"{rubric_path} dependency names an unknown requirement")
        dependencies.append(dependency)
    model_free_output = directory / "model-free-output.md"
    if not model_free_output.is_file():
        raise CaseError(f"{directory} has no model-free planner output")
    return Scenario(
        identifier=identifier,
        directory=directory,
        specification_directory=specs.relative_to(repository).as_posix(),
        expected_approval=expected,
        minimum_unresolved=minimum,
        required_signal_groups=groups,
        forbidden_terms=_strings(rubric["forbidden_terms"], "forbidden_terms"),
        requirements=tuple(requirements),
        dependencies=tuple(dependencies),
        model_free_output=model_free_output,
        fixture_unresolved=_strings(rubric["fixture_unresolved"], "fixture_unresolved"),
    )


def load_case(path: Path) -> EvaluationCase:
    case_path = path / "example.toml"
    try:
        value = tomllib.loads(case_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise CaseError(f"cannot load {case_path}: {error}") from error
    allowed = {"id", "title", "relation", "affected_requirements", "scenarios"}
    if set(value) != allowed:
        raise CaseError(
            f"{case_path} has unsupported or missing fields: {sorted(set(value) ^ allowed)}"
        )
    scenarios_raw = value["scenarios"]
    if not isinstance(scenarios_raw, list) or len(scenarios_raw) != 2:
        raise CaseError(f"{case_path} must define exactly two scenarios")
    scenarios = [_scenario(path, item) for item in scenarios_raw]
    if [item.identifier for item in scenarios] != ["base", "variant"]:
        raise CaseError(f"{case_path} scenarios must be base then variant")
    relation = value["relation"]
    if relation not in {"equivalent", "must-block"}:
        raise CaseError(f"{case_path}.relation is invalid")
    affected = _strings(value["affected_requirements"], "affected_requirements")
    known = {item.identifier for scenario in scenarios for item in scenario.requirements}
    if not set(affected) <= known:
        raise CaseError(f"{case_path} names an unknown affected requirement")
    return EvaluationCase(
        identifier=str(value["id"]),
        title=str(value["title"]),
        relation=relation,
        affected_requirements=affected,
        base=scenarios[0],
        variant=scenarios[1],
    )


def load_cases(root: Path | None = None) -> list[EvaluationCase]:
    root = root or Path(__file__).with_name("examples")
    cases = [load_case(path.parent) for path in sorted(root.glob("*/example.toml"))]
    identifiers = [item.identifier for item in cases]
    if not cases or len(set(identifiers)) != len(identifiers):
        raise CaseError("evaluation cases are absent or duplicate")
    return cases
