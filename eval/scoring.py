"""Deterministic planning metrics and metamorphic relations for GEPA."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, TemplateError, meta

from oxide.planning import PlanningInfrastructureError, planning_prompt_values
from oxide.prompt_templates import PromptTemplateError, render_prompt_source
from oxide.roadmap import (
    RoadmapError,
    canonical_source_anchor,
    canonical_source_text,
    parse_roadmap,
    proposed_stage_binding,
    render_roadmap_value,
)

from .cases import EvaluationCase, RequirementOracle, Scenario
from .identity import build_manifest
from .runners import PlannerRunner, QualityJudge

DETERMINISTIC_WEIGHT = 0.40
METAMORPHIC_WEIGHT = 0.15
CONSISTENCY_WEIGHT = 0.15
JUDGE_WEIGHT = 0.30


@dataclass
class ScenarioReport:
    identifier: str
    score: float
    metrics: dict[str, float]
    diagnostics: list[str]
    response: dict[str, Any] = field(repr=False)
    roadmap: dict[str, Any] | None = field(default=None, repr=False)
    requirement_stages: dict[str, tuple[str, ...]] = field(default_factory=dict)
    requirement_readiness: dict[str, tuple[str, ...]] = field(default_factory=dict)
    dependency_results: dict[str, bool] = field(default_factory=dict)

    @property
    def mechanical(self) -> bool:
        return self.metrics.get("mechanical_validity") == 1.0

    def semantic_signature(self) -> dict[str, Any]:
        return {
            "approval": bool(self.response.get("ready_for_approval")),
            "requirements": self.requirement_readiness,
            "dependencies": self.dependency_results,
        }

    def structural_signature(self) -> dict[str, Any]:
        """Return the roadmap decisions whose stability matters across identical runs."""
        if self.roadmap is None:
            return {}
        source_owners: dict[tuple[str, str, str], list[str]] = {}
        for invariant in self.roadmap["global_invariants"]:
            for source in invariant["sources"]:
                identity = _source_identity(source)
                source_owners.setdefault(identity, []).append(f"invariant:{invariant['id']}")
        for stage in self.roadmap["stages"]:
            for source in stage["source_specifications"]:
                identity = _source_identity(source)
                source_owners.setdefault(identity, []).append(f"stage:{stage['id']}")
        return {
            "stage_ids": tuple(stage["id"] for stage in self.roadmap["stages"]),
            "dependencies": tuple(
                (stage["id"], tuple(stage["dependencies"])) for stage in self.roadmap["stages"]
            ),
            "readiness": tuple(
                (stage["id"], stage["readiness"]) for stage in self.roadmap["stages"]
            ),
            "invariants": tuple(
                (
                    invariant["id"],
                    canonical_source_text(invariant["statement"]),
                    tuple(
                        stage["id"]
                        for stage in self.roadmap["stages"]
                        if invariant["id"] in stage["applicable_global_invariants"]
                    ),
                )
                for invariant in self.roadmap["global_invariants"]
            ),
            "source_owners": tuple(
                (identity, tuple(sorted(owners)))
                for identity, owners in sorted(source_owners.items())
            ),
            "requirement_stages": tuple(sorted(self.requirement_stages.items())),
            "semantic_content": tuple(
                (
                    stage["id"],
                    canonical_source_text(stage["outcome"]),
                    tuple(canonical_source_text(item) for item in stage["included_scope"]),
                    tuple(canonical_source_text(item) for item in stage["excluded_scope"]),
                    tuple(canonical_source_text(item) for item in stage["implementation_goals"]),
                    tuple(canonical_source_text(item) for item in stage["verification_goals"]),
                )
                for stage in self.roadmap["stages"]
            ),
            "approval": bool(self.response.get("ready_for_approval")),
        }


@dataclass
class CaseReport:
    identifier: str
    score: float
    deterministic_score: float
    metamorphic_score: float
    consistency_score: float
    judge_score: float | None
    base: ScenarioReport
    variant: ScenarioReport
    diagnostics: list[str]
    judge_details: dict[str, Any] | None = None

    def side_info(self) -> dict[str, Any]:
        return {
            "case": self.identifier,
            "score": round(self.score, 6),
            "objectives": {
                "deterministic_correctness": round(self.deterministic_score, 6),
                "metamorphic_robustness": round(self.metamorphic_score, 6),
                "repeat_run_consistency": round(self.consistency_score, 6),
                **(
                    {"llm_quality": round(self.judge_score, 6)}
                    if self.judge_score is not None
                    else {}
                ),
            },
            "base_metrics": self.base.metrics,
            "variant_metrics": self.variant.metrics,
            "diagnostics": self.diagnostics,
            **({"judge": self.judge_details} if self.judge_details else {}),
        }


_RESPONSE_FIELDS = {
    "message",
    "ready_for_approval",
    "complete_specification_corpus",
    "faithful_to_specifications",
    "unresolved",
    "roadmap",
}
_VACUOUS_WORDS = ("ensures true", "todo", "tbd", "proof later", "verify later")
_OXIDE_POLICY_ID = "oxide-verification-policy"
_OXIDE_POLICY_STATEMENT = (
    "Production logic has meaningful contracts, component refinement, complete coverage, "
    "and exact-tree composition; trusted effects remain narrow and policy-free."
)
_PHASE_OWNED_NARRATIVE_FIELDS = ("outcome", "included_scope", "implementation_goals")
_GOAL_TOKEN = re.compile(r"[a-z0-9]+")
_GENERIC_GOAL_TOKENS = {
    "acceptance",
    "check",
    "checks",
    "confirm",
    "contract",
    "demonstrate",
    "ensure",
    "formal",
    "goal",
    "goals",
    "proof",
    "prove",
    "qualification",
    "qualify",
    "test",
    "tests",
    "validate",
    "verification",
    "verify",
    "verus",
}


def _source_identity(reference: dict[str, str]) -> tuple[str, str, str]:
    """Return the strict semantic identity of one source citation.

    Source paths name authority and therefore remain byte-for-byte exact. Heading
    and requirement presentation use the same canonical Markdown projection as
    roadmap qualification: wrapping and unordered-list glyphs are cosmetic, while
    nesting, ordered ordinals, checkbox state, quotes, tables, and code remain
    semantic. Citation containment is deliberately broader than identity and must
    not be used here.
    """
    return (
        reference["path"],
        canonical_source_anchor(reference["anchor"]),
        canonical_source_text(reference["requirement"]),
    )


def template_variables(source: str) -> frozenset[str]:
    try:
        tree = Environment().parse(source)
    except TemplateError as error:
        raise ValueError(f"candidate is not valid Jinja: {error}") from error
    return frozenset(meta.find_undeclared_variables(tree))


def _response_shape(response: object) -> str | None:
    if not isinstance(response, dict) or set(response) != _RESPONSE_FIELDS:
        return "planner response does not match the required response fields"
    if (
        not isinstance(response["message"], str)
        or not isinstance(response["ready_for_approval"], bool)
        or not isinstance(response["complete_specification_corpus"], bool)
        or not isinstance(response["faithful_to_specifications"], bool)
        or not isinstance(response["roadmap"], dict)
        or not isinstance(response["unresolved"], list)
        or any(not isinstance(item, str) for item in response["unresolved"])
    ):
        return "planner response contains values of the wrong type"
    return None


def _reachability(roadmap: dict[str, Any]) -> dict[str, set[str]]:
    direct = {stage["id"]: set(stage["dependencies"]) for stage in roadmap["stages"]}
    closure: dict[str, set[str]] = {}
    for identifier, dependencies in direct.items():
        found: set[str] = set()
        pending = list(dependencies)
        while pending:
            item = pending.pop()
            if item in found:
                continue
            found.add(item)
            pending.extend(direct[item])
        closure[identifier] = found
    return closure


def _mean(values: list[float], empty: float = 1.0) -> float:
    return sum(values) / len(values) if values else empty


def _output_economy(roadmap: dict[str, Any]) -> tuple[float, list[str]]:
    """Detect only unambiguous verbatim duplication of phase-owned narrative.

    Absolute length and phase count are deliberately excluded: both can grow for
    faithful reasons, and rewarding smaller values would reward semantic omission.
    Verification goals are also excluded because one cross-cutting proof obligation
    can legitimately apply to several components.  An outcome, scope item, or
    implementation goal repeated verbatim across phases, however, has multiple
    owners and is actionable without a ground-truth roadmap.
    """
    claims: list[tuple[str, str, str]] = []
    owners: dict[tuple[str, str], set[str]] = {}
    for stage in roadmap["stages"]:
        for field_name in _PHASE_OWNED_NARRATIVE_FIELDS:
            value = stage[field_name]
            values = [value] if isinstance(value, str) else value
            for item in values:
                key = (field_name, canonical_source_text(item))
                claims.append((field_name, key[1], stage["id"]))
                owners.setdefault(key, set()).add(stage["id"])

    values = [float(len(owners[(field_name, text)]) == 1) for field_name, text, _ in claims]
    collisions = [
        (field_name, sorted(stage_ids))
        for (field_name, _text), stage_ids in sorted(owners.items())
        if len(stage_ids) > 1
    ]
    diagnostics = [
        f"phase-owned {field_name} narrative is duplicated across {', '.join(stage_ids)}"
        for field_name, stage_ids in collisions[:5]
    ]
    if len(collisions) > 5:
        diagnostics.append(
            f"{len(collisions) - 5} additional phase-owned narrative duplications were omitted"
        )
    return _mean(values), diagnostics


def _reference_supports_requirement(
    reference: dict[str, str], requirement: RequirementOracle
) -> bool:
    """Match a representative oracle to an exact or enclosing source citation.

    Roadmap qualification has already established that the emitted citation is a
    contiguous span of the named source section.  A longer span can therefore
    own a representative requirement it wholly contains.  The inverse is not
    sufficient: a shorter citation may omit a condition or exception carried by
    the oracle.
    """
    return (
        reference["path"] == requirement.path
        and canonical_source_anchor(reference["anchor"])
        == canonical_source_anchor(requirement.anchor)
        and canonical_source_text(requirement.text)
        in canonical_source_text(reference["requirement"])
    )


def _goal_grounding(stage: dict[str, Any]) -> tuple[set[str], set[str]]:
    source_text = " ".join(reference["requirement"] for reference in stage["source_specifications"])
    owned_text = " ".join(
        [stage["outcome"], *stage["included_scope"], *stage["implementation_goals"]]
    )
    source_tokens = {
        token
        for token in _GOAL_TOKEN.findall(canonical_source_text(source_text).lower())
        if token not in _GENERIC_GOAL_TOKENS
    }
    copied_narrative = {
        canonical_source_text(item)
        for item in [
            stage["outcome"],
            *stage["included_scope"],
            *stage["excluded_scope"],
            *stage["implementation_goals"],
        ]
    }
    owned_tokens = {
        token
        for token in _GOAL_TOKEN.findall(canonical_source_text(owned_text).lower())
        if token not in _GENERIC_GOAL_TOKENS
    }
    return source_tokens | owned_tokens, copied_narrative


def _goal_is_meaningful(goal: str, grounding_tokens: set[str], copied_narrative: set[str]) -> bool:
    canonical = canonical_source_text(goal)
    lowered = canonical.lower()
    tokens = _GOAL_TOKEN.findall(lowered)
    return (
        len(tokens) >= 3
        and canonical not in copied_narrative
        and not any(marker in lowered for marker in _VACUOUS_WORDS)
        and any(token in grounding_tokens for token in tokens)
    )


def _has_meaningful_assurance_goal(stage: dict[str, Any]) -> bool:
    """Require a non-vacuous assurance goal grounded in the phase's owned semantics.

    Mechanical scoring deliberately does not infer whether a phase is production,
    empirical, or mixed from its prose.  The LLM judge assesses whether the chosen
    formal, empirical, or mixed assurance treatment is appropriate.
    """
    grounding_tokens, copied_narrative = _goal_grounding(stage)
    return any(
        _goal_is_meaningful(goal, grounding_tokens, copied_narrative)
        for goal in stage["verification_goals"]
    )


def _verification_policy_checks(roadmap: dict[str, Any], diagnostics: list[str]) -> list[float]:
    """Enforce universal policy coverage and meaningful phase-local assurance."""
    policy = [
        invariant
        for invariant in roadmap["global_invariants"]
        if invariant["id"] == _OXIDE_POLICY_ID
    ]
    exact_policy = (
        len(policy) == 1
        and policy[0]["statement"] == _OXIDE_POLICY_STATEMENT
        and policy[0]["sources"] == []
        and sum(not invariant["sources"] for invariant in roadmap["global_invariants"]) == 1
    )
    values = [float(exact_policy)]
    if not exact_policy:
        diagnostics.append(
            "roadmap must declare exactly one source-free oxide-verification-policy invariant "
            "with the mandated statement"
        )

    for stage in roadmap["stages"]:
        applies_policy = _OXIDE_POLICY_ID in stage["applicable_global_invariants"]
        values.append(float(applies_policy))
        if not applies_policy:
            diagnostics.append(f"phase {stage['id']!r} does not apply oxide-verification-policy")
        meaningful_goal = _has_meaningful_assurance_goal(stage)
        values.append(float(meaningful_goal))
        if not meaningful_goal:
            diagnostics.append(
                f"phase {stage['id']!r} lacks a source-grounded, non-vacuous "
                "verification or qualification goal"
            )
    return values


def evaluate_scenario(
    repository: Path,
    candidate: str,
    scenario: Scenario,
    runner: PlannerRunner,
) -> ScenarioReport:
    diagnostics: list[str] = []
    try:
        prompt = render_prompt_source(
            candidate,
            **planning_prompt_values(repository, scenario.specification_directory),
        )
        response = runner.run(prompt, scenario)
    except PlanningInfrastructureError:
        raise
    except (PromptTemplateError, RoadmapError, RuntimeError, OSError) as error:
        return ScenarioReport(
            scenario.identifier,
            0.0,
            {"response_shape": 0.0, "mechanical_validity": 0.0},
            [f"planning execution failed: {error}"],
            {},
        )
    shape_problem = _response_shape(response)
    if shape_problem:
        return ScenarioReport(
            scenario.identifier,
            0.0,
            {"response_shape": 0.0, "mechanical_validity": 0.0},
            [shape_problem],
            response if isinstance(response, dict) else {},
        )
    try:
        rendered = render_roadmap_value(response["roadmap"], "ROADMAP.md")
        roadmap = parse_roadmap(rendered, "ROADMAP.md")
    except RoadmapError as error:
        return ScenarioReport(
            scenario.identifier,
            0.05,
            {"response_shape": 1.0, "mechanical_validity": 0.0},
            [f"roadmap qualification failed: {error}"],
            response,
        )
    qualification_errors: list[str] = []
    for stage in roadmap["stages"]:
        try:
            proposed_stage_binding(repository, "ROADMAP.md", rendered, stage["id"], {})
        except RoadmapError as error:
            qualification_errors.append(f"phase {stage['id']!r}: {error}")
    if qualification_errors:
        return ScenarioReport(
            scenario.identifier,
            0.05,
            {"response_shape": 1.0, "mechanical_validity": 0.0},
            [f"roadmap qualification failed: {error}" for error in qualification_errors],
            response,
            roadmap,
        )

    requirement_stages: dict[str, tuple[str, ...]] = {}
    requirement_readiness: dict[str, tuple[str, ...]] = {}
    stage_by_id = {stage["id"]: stage for stage in roadmap["stages"]}
    for requirement in scenario.requirements:
        found: list[str] = []
        for stage in roadmap["stages"]:
            if any(
                _reference_supports_requirement(reference, requirement)
                for reference in stage["source_specifications"]
            ):
                found.append(stage["id"])
        requirement_stages[requirement.identifier] = tuple(found)
        requirement_readiness[requirement.identifier] = tuple(
            sorted({stage_by_id[identifier]["readiness"] for identifier in found})
        )
        if not found:
            diagnostics.append(f"requirement {requirement.identifier!r} has no stage disposition")
        elif not any(
            stage_by_id[identifier]["readiness"] in requirement.readiness for identifier in found
        ):
            diagnostics.append(
                f"requirement {requirement.identifier!r} has readiness "
                f"{requirement_readiness[requirement.identifier]}, expected one of {requirement.readiness}"
            )
        if len(found) > 1:
            diagnostics.append(
                f"requirement {requirement.identifier!r} is duplicated across {', '.join(found)}"
            )

    coverage_values = [
        1.0 if requirement_stages[item.identifier] else 0.0 for item in scenario.requirements
    ]
    readiness_values = [
        1.0
        if any(
            stage_by_id[identifier]["readiness"] in item.readiness
            for identifier in requirement_stages[item.identifier]
        )
        else 0.0
        for item in scenario.requirements
    ]
    duplicate_values = [
        1.0 if len(requirement_stages[item.identifier]) <= 1 else 0.0
        for item in scenario.requirements
    ]

    reachability = _reachability(roadmap)
    dependency_results: dict[str, bool] = {}
    for dependency in scenario.dependencies:
        before = requirement_stages[dependency.before]
        after = requirement_stages[dependency.after]
        passed = bool(before and after) and any(
            prior == later or prior in reachability[later] for prior in before for later in after
        )
        key = f"{dependency.before}->{dependency.after}"
        dependency_results[key] = passed
        if not passed:
            diagnostics.append(f"required ordering {key} is absent")

    unresolved = response["unresolved"]
    unresolved_aligned = (
        not unresolved
        if scenario.expected_approval
        else len(unresolved) >= scenario.minimum_unresolved
    )
    alignment_checks = [
        response["ready_for_approval"] is scenario.expected_approval,
        response["complete_specification_corpus"] is True,
        response["faithful_to_specifications"] is True,
        unresolved_aligned,
        roadmap["status"] == ("ready" if scenario.expected_approval else "draft"),
    ]
    if not all(alignment_checks):
        diagnostics.append("approval/readiness flags do not match the scenario's contractibility")

    searchable = canonical_source_text(
        json.dumps(
            {
                "message": response["message"],
                "unresolved": unresolved,
                "roadmap": roadmap,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    ).lower()
    signal_values: list[float] = []
    for group in scenario.required_signal_groups:
        passed = all(term.lower() in searchable for term in group)
        signal_values.append(float(passed))
        if not passed:
            diagnostics.append("missing ambiguity signal: " + " + ".join(group))
    forbidden_values: list[float] = []
    for term in scenario.forbidden_terms:
        passed = term.lower() not in searchable
        forbidden_values.append(float(passed))
        if not passed:
            diagnostics.append(f"unsupported product concept appeared: {term}")

    policy_values = _verification_policy_checks(roadmap, diagnostics)

    source_owners: dict[tuple[str, str, str], list[str]] = {}
    for invariant in roadmap["global_invariants"]:
        for source in invariant["sources"]:
            identity = _source_identity(source)
            source_owners.setdefault(identity, []).append(f"invariant:{invariant['id']}")
    for stage in roadmap["stages"]:
        for source in stage["source_specifications"]:
            identity = _source_identity(source)
            source_owners.setdefault(identity, []).append(f"phase:{stage['id']}")
    ownership_values: list[float] = []
    for identity, owners in source_owners.items():
        invariant_owners = [owner for owner in owners if owner.startswith("invariant:")]
        phase_owners = [owner for owner in owners if owner.startswith("phase:")]
        # A cross-cutting rule may be declared once as an invariant and established by one
        # implementation phase. What creates unstable duplicate work is more than one invariant
        # or more than one phase claiming the same source requirement.
        valid = len(invariant_owners) <= 1 and len(phase_owners) <= 1
        ownership_values.append(float(valid))
        if not valid:
            diagnostics.append(
                "source requirement has multiple roadmap owners: "
                + f"{identity[0]}#{identity[1]} -> {', '.join(owners)}"
            )

    output_economy, economy_diagnostics = _output_economy(roadmap)
    diagnostics.extend(economy_diagnostics)

    metrics = {
        "response_shape": 1.0,
        "mechanical_validity": 1.0,
        "requirement_coverage": _mean(coverage_values),
        "readiness_calibration": _mean(readiness_values),
        "single_disposition": _mean(duplicate_values),
        "source_ownership": _mean(ownership_values),
        "output_economy": output_economy,
        "dependency_correctness": _mean([float(value) for value in dependency_results.values()]),
        "approval_alignment": _mean([float(value) for value in alignment_checks]),
        "ambiguity_signals": _mean(signal_values),
        "verification_policy": _mean(policy_values),
        "non_invention_guard": _mean(forbidden_values),
    }
    score = (
        0.20 * metrics["mechanical_validity"]
        + 0.18 * metrics["requirement_coverage"]
        + 0.12 * metrics["readiness_calibration"]
        + 0.04 * metrics["single_disposition"]
        + 0.06 * metrics["source_ownership"]
        + 0.02 * metrics["output_economy"]
        + 0.10 * metrics["dependency_correctness"]
        + 0.10 * metrics["approval_alignment"]
        + 0.07 * metrics["ambiguity_signals"]
        + 0.07 * metrics["verification_policy"]
        + 0.04 * metrics["non_invention_guard"]
    )
    score = min(1.0, max(0.0, score))
    return ScenarioReport(
        scenario.identifier,
        score,
        metrics,
        diagnostics,
        response,
        roadmap,
        requirement_stages,
        requirement_readiness,
        dependency_results,
    )


def _metamorphic(
    case: EvaluationCase, base: ScenarioReport, variant: ScenarioReport
) -> tuple[float, list[str]]:
    diagnostics: list[str] = []
    if not base.mechanical or not variant.mechanical:
        return 0.0, ["metamorphic relation cannot be evaluated because a roadmap is invalid"]
    if case.relation == "equivalent":
        left, right = base.semantic_signature(), variant.semantic_signature()
        checks = [
            left["approval"] == right["approval"],
            left["requirements"] == right["requirements"],
            left["dependencies"] == right["dependencies"],
        ]
        if not all(checks):
            diagnostics.append("format-only source changes altered the semantic plan")
        return _mean([float(value) for value in checks]), diagnostics

    affected_checks: list[bool] = []
    for identifier in case.affected_requirements:
        base_ready = "ready" in base.requirement_readiness.get(identifier, ())
        variant_ready = "ready" in variant.requirement_readiness.get(identifier, ())
        affected_checks.extend([base_ready, not variant_ready])
        if not base_ready or variant_ready:
            diagnostics.append(
                f"affected requirement {identifier!r} did not move from ready to non-ready"
            )
    unaffected_checks: list[bool] = []
    for identifier in sorted(set(base.requirement_readiness) - set(case.affected_requirements)):
        passed = base.requirement_readiness.get(
            identifier, ()
        ) == variant.requirement_readiness.get(identifier, ())
        unaffected_checks.append(passed)
        if not passed:
            diagnostics.append(
                f"unaffected requirement {identifier!r} changed readiness; "
                "phase contractibility must not propagate through dependencies"
            )
    checks = [
        *affected_checks,
        *unaffected_checks,
        variant.response.get("ready_for_approval") is case.variant.expected_approval,
        (
            not variant.response.get("unresolved", [])
            if case.variant.expected_approval
            else len(variant.response.get("unresolved", [])) >= case.variant.minimum_unresolved
        ),
        variant.metrics.get("ambiguity_signals") == 1.0,
    ]
    if not all(checks):
        diagnostics.append(
            "the semantic gap was not isolated to its smallest affected readiness unit"
        )
    return _mean([float(value) for value in checks]), diagnostics


def _repeat_consistency(
    baseline: ScenarioReport, repetitions: list[ScenarioReport]
) -> tuple[float, list[str]]:
    """Score planning decisions that should be stable for one identical corpus."""
    if not repetitions:
        return 1.0, []
    diagnostics: list[str] = []
    wanted = baseline.structural_signature()
    if not baseline.mechanical or not wanted:
        return 0.0, ["repeat-run consistency cannot be evaluated because the baseline is invalid"]
    labels = {
        "stage_ids": "phase identities or order",
        "dependencies": "dependency graph",
        "readiness": "phase readiness",
        "invariants": "global-invariant statement or placement",
        "source_owners": "source-requirement ownership",
        "requirement_stages": "representative requirement disposition",
        "semantic_content": "outcome, scope, implementation, or verification content",
        "approval": "approval disposition",
    }
    values: list[float] = []
    for ordinal, repetition in enumerate(repetitions, 2):
        actual = repetition.structural_signature()
        if not repetition.mechanical or not actual:
            values.extend(0.0 for _ in labels)
            diagnostics.append(f"identical run {ordinal} did not produce a valid roadmap")
            continue
        for key, label in labels.items():
            passed = wanted[key] == actual[key]
            values.append(float(passed))
            if not passed:
                diagnostics.append(
                    f"identical run {ordinal} changed {label}"
                    + _repeat_signature_detail(key, wanted, actual)
                )
    return _mean(values), diagnostics


_REPEAT_DETAIL_LIMIT = 5


def _bounded_repeat_keys(values: set[str]) -> str:
    ordered = sorted(values)
    shown = ordered[:_REPEAT_DETAIL_LIMIT]
    suffix = (
        f", +{len(ordered) - _REPEAT_DETAIL_LIMIT} more"
        if len(ordered) > _REPEAT_DETAIL_LIMIT
        else ""
    )
    return "[" + ", ".join(shown) + suffix + "]"


def _changed_tuple_map_keys(
    wanted: tuple[Any, ...], actual: tuple[Any, ...], *, common_only: bool = False
) -> set[Any]:
    wanted_map = {item[0]: item[1:] for item in wanted}
    actual_map = {item[0]: item[1:] for item in actual}
    keys = set(wanted_map) & set(actual_map) if common_only else set(wanted_map) | set(actual_map)
    return {key for key in keys if wanted_map.get(key) != actual_map.get(key)}


def _source_owner_key(identity: tuple[str, str, str]) -> str:
    path, anchor, requirement = identity
    digest = hashlib.sha256(requirement.encode("utf-8")).hexdigest()[:8]
    return f"{path}#{anchor}@{digest}"


def _repeat_signature_detail(key: str, wanted: dict[str, Any], actual: dict[str, Any]) -> str:
    """Describe one structural drift compactly without copying roadmap prose."""
    if key == "stage_ids":
        wanted_ids = set(wanted[key])
        actual_ids = set(actual[key])
        added = _bounded_repeat_keys(actual_ids - wanted_ids)
        removed = _bounded_repeat_keys(wanted_ids - actual_ids)
        order = (
            "; order changed" if not (actual_ids - wanted_ids or wanted_ids - actual_ids) else ""
        )
        return f" (added phase IDs: {added}; removed phase IDs: {removed}{order})"
    if key in {"dependencies", "readiness"}:
        changed = {
            str(item)
            for item in _changed_tuple_map_keys(wanted[key], actual[key], common_only=True)
        }
        return f" (changed keys: {_bounded_repeat_keys(changed)})"
    if key == "source_owners":
        changed = {
            _source_owner_key(identity)
            for identity in _changed_tuple_map_keys(wanted[key], actual[key])
        }
        return f" (changed keys: {_bounded_repeat_keys(changed)})"
    if key in {"invariants", "requirement_stages", "semantic_content"}:
        changed = {str(item) for item in _changed_tuple_map_keys(wanted[key], actual[key])}
        return f" (changed keys: {_bounded_repeat_keys(changed)})"
    return ""


class EvaluationHarness:
    """GEPA-compatible evaluator returning a score and actionable side information."""

    def __init__(
        self,
        repository: Path,
        seed_template: str,
        cases: list[EvaluationCase],
        runner: PlannerRunner,
        judge: QualityJudge | None = None,
        replicates: int = 2,
    ):
        self.repository = repository.resolve()
        self.cases = {case.identifier: case for case in cases}
        self.runner = runner
        self.judge = judge
        if replicates < 1:
            raise ValueError("planning evaluation requires at least one replicate")
        self.replicates = replicates
        self.required_variables = template_variables(seed_template)
        self.seed_template = seed_template
        self._cache: dict[tuple[str, str, str, int], ScenarioReport] = {}
        self._manifest = self._build_manifest(proposer=None)
        self.evaluation_fingerprint = str(self._manifest["fingerprint"])

    @staticmethod
    def weights() -> dict[str, float]:
        return {
            "deterministic": DETERMINISTIC_WEIGHT,
            "metamorphic": METAMORPHIC_WEIGHT,
            "repeat_consistency": CONSISTENCY_WEIGHT,
            "judge": JUDGE_WEIGHT,
            "without_judge_deterministic": 0.60,
            "without_judge_metamorphic": 0.20,
            "without_judge_repeat_consistency": 0.20,
            "invalid_mechanical_cap": 0.20,
            "invalid_verification_policy_cap": 0.20,
            "scenario_output_economy": 0.02,
            "scenario_source_ownership": 0.06,
        }

    def _build_manifest(self, proposer: object | None) -> dict[str, Any]:
        return build_manifest(
            self.repository,
            cases=self.cases.values(),
            seed_template=self.seed_template,
            runner=self.runner,
            judge=self.judge,
            proposer=proposer,
            replicates=self.replicates,
            weights=self.weights(),
        )

    def bind_optimizer(self, proposer: object | None) -> dict[str, Any]:
        """Freeze the exact evaluation identity before GEPA reads cache or state."""
        manifest = self._build_manifest(proposer)
        fingerprint = str(manifest["fingerprint"])
        if fingerprint != self.evaluation_fingerprint:
            self._cache.clear()
        self._manifest = manifest
        self.evaluation_fingerprint = fingerprint
        return json.loads(json.dumps(manifest))

    def manifest(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._manifest))

    def _scenario(self, candidate: str, scenario: Scenario, replicate: int = 0) -> ScenarioReport:
        digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        key = (self.evaluation_fingerprint, digest, str(scenario.directory), replicate)
        if key not in self._cache:
            self._cache[key] = evaluate_scenario(self.repository, candidate, scenario, self.runner)
        return self._cache[key]

    def evaluate(self, candidate: str, case: EvaluationCase) -> CaseReport:
        variables = template_variables(candidate)
        if variables != self.required_variables:
            missing = sorted(self.required_variables - variables)
            added = sorted(variables - self.required_variables)
            diagnostic = f"template variable contract changed; missing={missing}, added={added}"
            empty = ScenarioReport(
                "not-run",
                0.0,
                {"template_contract": 0.0, "mechanical_validity": 0.0},
                [diagnostic],
                {},
            )
            return CaseReport(
                case.identifier,
                0.0,
                0.0,
                0.0,
                0.0,
                None,
                empty,
                empty,
                [diagnostic],
            )
        base = self._scenario(candidate, case.base)
        if not base.mechanical:
            skipped = ScenarioReport(
                case.variant.identifier,
                0.0,
                {"not_run": 1.0},
                ["skipped because the baseline proposal failed mechanical admission"],
                {},
            )
            diagnostics = [
                *(f"base: {item}" for item in base.diagnostics),
                *(f"variant: {item}" for item in skipped.diagnostics),
                "repeat runs and quality judging were skipped after the fatal baseline failure",
            ]
            return CaseReport(
                case.identifier,
                0.0,
                0.0,
                0.0,
                0.0,
                None,
                base,
                skipped,
                diagnostics,
            )
        variant = self._scenario(candidate, case.variant)
        repetitions = [
            self._scenario(candidate, case.base, replicate)
            for replicate in range(1, self.replicates)
        ]
        deterministic = (base.score + variant.score) / 2.0
        metamorphic, relation_diagnostics = _metamorphic(case, base, variant)
        consistency, consistency_diagnostics = _repeat_consistency(base, repetitions)
        judge_score: float | None = None
        judge_details: dict[str, Any] | None = None
        if self.judge is not None and base.response and variant.response:
            judge_score, judge_details = self.judge.score(case, base.response, variant.response)
        if judge_score is not None and base.mechanical and variant.mechanical:
            score = (
                DETERMINISTIC_WEIGHT * deterministic
                + METAMORPHIC_WEIGHT * metamorphic
                + CONSISTENCY_WEIGHT * consistency
                + JUDGE_WEIGHT * judge_score
            )
        else:
            score = 0.60 * deterministic + 0.20 * metamorphic + 0.20 * consistency
        if not base.mechanical or not variant.mechanical:
            score = min(score, 0.20)
        if (
            base.metrics.get("verification_policy", 0.0) < 1.0
            or variant.metrics.get("verification_policy", 0.0) < 1.0
        ):
            score = min(score, 0.20)
        diagnostics = [
            *(f"base: {item}" for item in base.diagnostics),
            *(f"variant: {item}" for item in variant.diagnostics),
            *relation_diagnostics,
            *consistency_diagnostics,
        ]
        return CaseReport(
            case.identifier,
            score,
            deterministic,
            metamorphic,
            consistency,
            judge_score,
            base,
            variant,
            diagnostics,
            judge_details,
        )

    def __call__(self, candidate: object, example: object) -> tuple[float, dict[str, Any]]:
        if not isinstance(candidate, dict) or set(candidate) != {"planning_prompt"}:
            return 0.0, {"diagnostics": ["candidate must contain only planning_prompt"]}
        if not isinstance(example, dict) or set(example) != {
            "case_id",
            "evaluation_fingerprint",
        }:
            return 0.0, {
                "diagnostics": ["dataset example must contain case_id and evaluation_fingerprint"]
            }
        if example["evaluation_fingerprint"] != self.evaluation_fingerprint:
            return 0.0, {"diagnostics": ["dataset evaluation fingerprint is stale or foreign"]}
        case = self.cases.get(str(example["case_id"]))
        if case is None:
            return 0.0, {"diagnostics": ["unknown evaluation case"]}
        try:
            report = self.evaluate(str(candidate["planning_prompt"]), case)
        except (ValueError, TemplateError) as error:
            return 0.0, {"case": case.identifier, "diagnostics": [str(error)]}
        return report.score, report.side_info()

    def dataset(self) -> list[dict[str, str]]:
        return [
            {
                "case_id": identifier,
                "evaluation_fingerprint": self.evaluation_fingerprint,
            }
            for identifier in self.cases
        ]
