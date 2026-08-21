from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.cases import load_cases
from eval.gepa_harness import OBJECTIVE, optimize_prompt
from eval.identity import EvaluationIdentityError, build_manifest, qualify_run_directory
from eval.runners import (
    CodexPlannerRunner,
    CodexPromptProposer,
    CodexQualityJudge,
    FixturePlannerRunner,
    _judge_source_bundle,
)
from eval.scoring import (
    CONSISTENCY_WEIGHT,
    DETERMINISTIC_WEIGHT,
    JUDGE_WEIGHT,
    METAMORPHIC_WEIGHT,
    EvaluationHarness,
    _localized_structure_score,
    _reference_supports_requirement,
    _relative_source_identity,
    _repeat_consistency,
    _source_identity,
    evaluate_scenario,
)
from oxide.planning import PlanningInfrastructureError

REPOSITORY = Path(__file__).resolve().parents[2]
SEED_PATH = REPOSITORY / "src" / "oxide" / "prompts" / "planning.md.j2"


def _harness() -> tuple[str, EvaluationHarness]:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()
    return seed, EvaluationHarness(REPOSITORY, seed, cases, FixturePlannerRunner())


def test_gepa_objective_prioritizes_correctness_without_compactness_incentives() -> None:
    objective = " ".join(OBJECTIVE.split()).lower()

    assert "treat these priorities as lexicographic" in objective
    assert "a lower-priority gain must never weaken a higher-priority invariant" in objective
    assert "success on one corpus must not compensate for failure on another" in objective
    assert "each variable's meaning and control-flow role" in objective
    assert "exactly one visible, source-backed disposition" in objective
    assert "honest top-level approval and unresolved state" in objective
    assert "make the semantic projection invariant and repeatable" in objective
    assert "approval disposition" in objective
    assert (
        "compactness, concision, prompt or roadmap length, list length, citation count, and phase count are not quality objectives"
        in objective
    )
    assert "concise canonical roadmap structure" not in objective
    assert "maximize" not in objective
    assert "verbatim duplicate phase-owned narrative" in objective
    assert "do not game coverage or consistency" in objective


def _roadmap_stage(roadmap: dict, phase_id: str) -> dict:
    matches = [stage for stage in roadmap["stages"] if stage["id"] == phase_id]
    assert len(matches) == 1
    return matches[0]


def _replace_phase_readiness(roadmap: dict, phase_id: str, before: str, after: str) -> dict:
    phase = _roadmap_stage(roadmap, phase_id)
    assert phase["readiness"] == before
    phase["readiness"] = after
    return roadmap


def _replace_roadmap_string(value: object, before: str, after: str) -> int:
    replacements = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if item == before:
                value[key] = after
                replacements += 1
            else:
                replacements += _replace_roadmap_string(item, before, after)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            if item == before:
                value[index] = after
                replacements += 1
            else:
                replacements += _replace_roadmap_string(item, before, after)
    return replacements


BENCHMARK_CASES = {
    "agent-message-board",
    "collaborative-document",
    "transactional-reservation",
}
SMOKE_CASES = {"durable-counter", "private-notes", "retry-queue"}


def test_suite_contains_smoke_and_journal_scale_cases() -> None:
    cases = load_cases()

    assert {case.identifier for case in cases} == SMOKE_CASES | BENCHMARK_CASES
    relations = {case.identifier: case.relation for case in cases}
    assert relations["durable-counter"] == "equivalent"
    assert all(
        relation == "localized-block"
        for identifier, relation in relations.items()
        if identifier != "durable-counter"
    )


def test_benchmark_corpora_are_large_domain_specs_without_planning_labels() -> None:
    examples = REPOSITORY / "eval" / "examples"
    planning_label = re.compile(r"\b(?:roadmap|stages?)\b", re.IGNORECASE)

    for identifier in BENCHMARK_CASES:
        files = sorted((examples / identifier / "base" / "specs").glob("*.md"))
        assert [path.name for path in files] == ["DEVELOPMENT.md", "PRODUCT.md", "RESEARCH.md"]
        texts = [path.read_text(encoding="utf-8") for path in files]
        line_count = sum(len(text.splitlines()) for text in texts)
        assert 1_500 <= line_count <= 3_000
        assert not any(planning_label.search(text) for text in texts)


def test_fixture_outputs_score_every_deterministic_and_metamorphic_objective() -> None:
    seed, harness = _harness()

    reports = [harness.evaluate(seed, case) for case in harness.cases.values()]

    assert len(reports) == len(SMOKE_CASES | BENCHMARK_CASES)
    assert all(report.score == 1.0 for report in reports)
    assert all(not report.diagnostics for report in reports)
    assert all(report.base.mechanical and report.variant.mechanical for report in reports)
    assert all(
        "oxide-verification-policy" in stage["applicable_global_invariants"]
        for report in reports
        for scenario in (report.base, report.variant)
        for stage in scenario.roadmap["stages"]
    )


def test_fixture_runner_uses_the_same_structured_roadmap_transport_as_codex() -> None:
    case = next(item for item in load_cases() if item.identifier == "durable-counter")

    response = FixturePlannerRunner().run("rendered prompt", case.base)

    assert "roadmap_markdown" not in response
    assert isinstance(response["roadmap"], dict)
    assert response["roadmap"]["schema"] == 1
    requirements = [
        source["requirement"]
        for stage in response["roadmap"]["stages"]
        for source in stage["source_specifications"]
    ]
    assert requirements
    assert all(not requirement.endswith("'") for requirement in requirements)


def test_judge_sees_complete_base_corpus_once_plus_only_variant_delta() -> None:
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")

    bundle = _judge_source_bundle(case)

    assert bundle.count("# Agent Message Board Product Specification") == 1
    assert bundle.count("# Agent Message Board Development Specification") == 1
    assert bundle.count("# Agent Message Board Research Specification") == 1
    assert "VARIANT SOURCE specs/CLAIM-CONFLICT.md" in bundle


def test_judge_is_told_readiness_is_local_and_does_not_propagate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval import runners

    captured: dict[str, str] = {}

    class RecordingSession:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        def start(self, prompt, schema):
            del schema
            captured["prompt"] = prompt
            return {
                "faithfulness": 4,
                "coverage": 4,
                "decomposition": 4,
                "readability": 4,
                "reason": "qualified",
            }

    monkeypatch.setattr(runners, "CodexSessionAgent", RecordingSession)
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")
    fixture = FixturePlannerRunner()
    base_response = fixture.run("rendered prompt", case.base)
    variant_response = fixture.run("rendered prompt", case.variant)

    CodexQualityJudge(REPOSITORY).score(case, base_response, variant_response)

    prompt = captured["prompt"]
    assert "Phase readiness describes only" in prompt
    assert "never propagate readiness" in prompt
    assert "may remain approval-ready" in prompt
    assert "Evaluate the BASE response against the base sources only" in prompt
    assert "exactly one source-free" in prompt
    assert "apply it to every phase without classifying phases by keywords" in prompt
    assert "both where a phase mixes those responsibilities" in prompt
    assert "Measurements must not be" in prompt
    assert "never lower readability merely because the trace payload is long" in prompt
    assert "BASE RENDERED HUMAN ROADMAP\n# Roadmap" in prompt
    assert "BASE REQUIRED TRACE PAYLOAD" in prompt
    base_human = prompt.split("BASE RENDERED HUMAN ROADMAP\n", 1)[1].split(
        "\n\nBASE REQUIRED TRACE PAYLOAD", 1
    )[0]
    source_requirement = base_response["roadmap"]["stages"][0]["source_specifications"][0][
        "requirement"
    ]
    assert source_requirement not in base_human
    assert source_requirement in prompt.split("BASE REQUIRED TRACE PAYLOAD", 1)[1]
    assert 'Affected requirements: ["claim-winner"]' in prompt


def test_judge_materially_influences_score_without_displacing_hard_guards() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class ZeroJudge:
        def score(self, case, base_response, variant_response):
            del case, base_response, variant_response
            return 0.0, {"reason": "intentionally harsh fixture judge"}

    harness = EvaluationHarness(REPOSITORY, seed, cases, FixturePlannerRunner(), ZeroJudge())
    report = harness.evaluate(seed, harness.cases["durable-counter"])

    assert DETERMINISTIC_WEIGHT + METAMORPHIC_WEIGHT + CONSISTENCY_WEIGHT + JUDGE_WEIGHT == 1.0
    assert JUDGE_WEIGHT == 0.30
    assert report.deterministic_score == 1.0
    assert report.metamorphic_score == 1.0
    assert report.consistency_score == 1.0
    assert report.judge_score == 0.0
    assert report.score == DETERMINISTIC_WEIGHT + METAMORPHIC_WEIGHT + CONSISTENCY_WEIGHT


def test_identical_input_replicates_penalize_structural_variance() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class CyclingRunner(FixturePlannerRunner):
        base_calls = 0

        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier == "base":
                self.base_calls += 1
                if self.base_calls % 2 == 0:
                    stage = _roadmap_stage(response["roadmap"], "counter-core")
                    assert stage["outcome"] == (
                        "A durable checked counter is available through its core API."
                    )
                    stage["outcome"] = (
                        "The core API exposes a durable counter with checked updates."
                    )
            return response

    runner = CyclingRunner()
    harness = EvaluationHarness(
        REPOSITORY,
        seed,
        cases,
        runner,
        replicates=2,
    )
    report = harness.evaluate(seed, harness.cases["durable-counter"])

    assert runner.base_calls == 2
    assert report.base.mechanical and report.variant.mechanical
    assert report.consistency_score < 1.0
    assert report.score < 1.0
    assert any("identical run 2 changed" in item for item in report.diagnostics)
    assert any("outcome, scope" in item for item in report.diagnostics)


def test_repeat_consistency_reports_bounded_structural_delta_keys() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "durable-counter")
    baseline = evaluate_scenario(REPOSITORY, seed, case.base, FixturePlannerRunner())
    assert baseline.roadmap is not None
    repeated_roadmap = copy.deepcopy(baseline.roadmap)
    core = _roadmap_stage(repeated_roadmap, "counter-core")
    adapter = _roadmap_stage(repeated_roadmap, "http-adapter")
    moved_source = next(
        source for source in core["source_specifications"] if source["anchor"] == "Updates"
    )
    core["source_specifications"].remove(moved_source)
    adapter["source_specifications"].append(moved_source)
    core["id"] = "counter-v2"
    adapter["dependencies"] = ["counter-v2"]
    adapter["readiness"] = "blocked"
    repetition = replace(baseline, roadmap=repeated_roadmap)

    consistency, diagnostics = _repeat_consistency(baseline, [repetition])

    assert consistency < 1.0
    assert any(
        "added phase IDs: [counter-v2]; removed phase IDs: [counter-core]" in item
        for item in diagnostics
    )
    assert any("dependency graph (changed keys: [http-adapter])" in item for item in diagnostics)
    assert any("phase readiness (changed keys: [http-adapter])" in item for item in diagnostics)
    owner_diagnostic = next(item for item in diagnostics if "source-requirement ownership" in item)
    assert "#Updates@" in owner_diagnostic
    assert all(len(item) < 500 for item in diagnostics)


def test_source_identity_ignores_only_cosmetic_markdown_presentation() -> None:
    canonical = {
        "path": "docs/specs/PRODUCT.md",
        "anchor": "Rules",
        "requirement": "- [x] First requirement wraps across lines.\n  - Nested witness.",
    }
    cosmetic = {
        "path": "docs/specs/PRODUCT.md",
        "anchor": "## Rules ##",
        "requirement": "* [X] **First** requirement wraps\n    across lines.\n    + Nested witness.",
    }

    assert _source_identity(canonical) == _source_identity(cosmetic)
    assert _source_identity(canonical) != _source_identity(
        {**cosmetic, "path": "docs/specs/DEVELOPMENT.md"}
    )
    assert _source_identity(canonical) != _source_identity(
        {
            **cosmetic,
            "requirement": "- [ ] First requirement wraps across lines.\n  - Nested witness.",
        }
    )
    assert _source_identity(canonical) != _source_identity(
        {
            **cosmetic,
            "requirement": "- [x] First requirement wraps across lines.\n- Nested witness.",
        }
    )
    assert _source_identity(canonical) != _source_identity(
        {**cosmetic, "requirement": "> First requirement wraps across lines. Nested witness."}
    )


def test_parsed_source_identity_preserves_one_pass_canonical_values() -> None:
    reference = {
        "path": "docs/specs/PRODUCT.md",
        "anchor": "&amp;",
        "requirement": "AT&amp;amp;T must remain supported.",
    }

    assert _source_identity(reference, anchor_is_canonical=True) == (
        "docs/specs/PRODUCT.md",
        "&amp;",
        "AT&amp;T must remain supported.",
    )
    assert _source_identity(reference, anchor_is_canonical=True) != _source_identity(
        {**reference, "anchor": "&"}, anchor_is_canonical=True
    )
    assert _source_identity(reference, anchor_is_canonical=True) != _source_identity(
        {**reference, "requirement": "AT&amp;T must remain supported."},
        anchor_is_canonical=True,
    )
    assert _reference_supports_requirement(
        reference,
        SimpleNamespace(
            path="docs/specs/PRODUCT.md",
            anchor="&amp;amp;",
            text="AT&amp;amp;T must remain supported.",
        ),
    )


def test_cosmetic_source_formatting_does_not_create_repeat_run_drift() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "durable-counter")
    baseline = evaluate_scenario(REPOSITORY, seed, case.base, FixturePlannerRunner())
    assert baseline.roadmap is not None
    repeated_roadmap = copy.deepcopy(baseline.roadmap)
    source = next(
        item
        for item in repeated_roadmap["stages"][0]["source_specifications"]
        if item["anchor"] == "Updates" and item["requirement"].startswith("A client may add")
    )
    source["requirement"] = "A client may add a signed 64-bit delta\nto a named counter."
    repetition = replace(baseline, roadmap=repeated_roadmap)

    consistency, diagnostics = _repeat_consistency(baseline, [repetition])

    assert consistency == 1.0
    assert diagnostics == []


def test_semantic_global_invariant_statement_drift_loses_repeat_consistency() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "durable-counter")
    baseline = evaluate_scenario(REPOSITORY, seed, case.base, FixturePlannerRunner())
    assert baseline.roadmap is not None
    repeated_roadmap = copy.deepcopy(baseline.roadmap)
    invariant = next(
        item
        for item in repeated_roadmap["global_invariants"]
        if item["id"] == "oxide-verification-policy"
    )
    invariant["statement"] = (
        "Production logic has meaningful contracts, component refinement, complete coverage, "
        "and exact-tree composition; trusted effects may contain product policy."
    )
    repetition = replace(baseline, roadmap=repeated_roadmap)

    consistency, diagnostics = _repeat_consistency(baseline, [repetition])

    assert consistency < 1.0
    assert diagnostics == [
        (
            "identical run 2 changed global-invariant statement or placement "
            "(changed keys: [oxide-verification-policy])"
        )
    ]


def test_requirement_oracle_requires_source_path_anchor_and_text() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")
    requirement = case.base.requirements[0]
    assert requirement.path == "eval/examples/agent-message-board/base/specs/PRODUCT.md"
    wrong_path = replace(
        requirement,
        path="eval/examples/agent-message-board/base/specs/DEVELOPMENT.md",
    )
    scenario = replace(
        case.base,
        requirements=(wrong_path, *case.base.requirements[1:]),
    )

    report = evaluate_scenario(REPOSITORY, seed, scenario, FixturePlannerRunner())

    assert not report.requirement_stages[requirement.identifier]
    assert report.metrics["requirement_coverage"] < 1.0
    assert any(
        "immutable-record" in item and "no stage disposition" in item for item in report.diagnostics
    )


def test_requirement_oracle_accepts_a_longer_valid_source_span() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "durable-counter")

    class LongerCitationRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier == "base":
                stage = _roadmap_stage(response["roadmap"], "counter-core")
                path = "eval/examples/durable-counter/base/specs/PRODUCT.md"
                first = {
                    "path": path,
                    "anchor": "Updates",
                    "requirement": "A client may add a signed 64-bit delta to a named counter.",
                }
                second = {
                    "path": path,
                    "anchor": "Updates",
                    "requirement": (
                        "An update that would overflow the signed 64-bit range is rejected "
                        "without changing the counter."
                    ),
                }
                assert first in stage["source_specifications"]
                assert second in stage["source_specifications"]
                stage["source_specifications"] = [
                    source
                    for source in stage["source_specifications"]
                    if source != first and source != second
                ]
                stage["source_specifications"].append(
                    {
                        "path": path,
                        "anchor": "Updates",
                        "requirement": (
                            "A client may add a signed 64-bit delta to a named counter.\n\n"
                            "An update that would overflow the signed 64-bit range is rejected "
                            "without changing the counter."
                        ),
                    }
                )
            return response

    report = evaluate_scenario(REPOSITORY, seed, case.base, LongerCitationRunner())

    assert report.mechanical
    assert report.requirement_stages["update"] == ("counter-core",)
    assert report.requirement_stages["overflow"] == ("counter-core",)
    assert report.metrics["requirement_coverage"] == 1.0


def test_requirement_oracle_rejects_wrong_anchor_and_partial_source_span() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "durable-counter")
    update, overflow = case.base.requirements[:2]
    wrong_anchor = replace(update, anchor="Reads")
    broader_than_citation = replace(
        update,
        text=f"{update.text}\n\n{overflow.text}",
    )

    for oracle in (wrong_anchor, broader_than_citation):
        scenario = replace(
            case.base,
            requirements=(oracle, *case.base.requirements[2:]),
        )
        report = evaluate_scenario(REPOSITORY, seed, scenario, FixturePlannerRunner())

        assert report.mechanical
        assert not report.requirement_stages[update.identifier]
        assert report.metrics["requirement_coverage"] < 1.0
        assert any(
            "requirement 'update' has no stage disposition" in item for item in report.diagnostics
        )


def test_verbatim_phase_ownership_collision_loses_output_economy_only() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "durable-counter")

    class DuplicatedNarrativeRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier == "base":
                replacements = _replace_roadmap_string(
                    response["roadmap"],
                    "Define and implement the deferred HTTP adapter.",
                    "Implement checked updates, reads, and durable recovery together.",
                )
                assert replacements == 1
            return response

    baseline = evaluate_scenario(REPOSITORY, seed, case.base, FixturePlannerRunner())
    repeated = evaluate_scenario(REPOSITORY, seed, case.base, DuplicatedNarrativeRunner())

    assert repeated.mechanical
    assert repeated.metrics["requirement_coverage"] == baseline.metrics["requirement_coverage"]
    assert repeated.metrics["source_ownership"] == baseline.metrics["source_ownership"]
    assert baseline.metrics["output_economy"] == 1.0
    assert repeated.metrics["output_economy"] < 1.0
    assert repeated.score < baseline.score
    assert any(
        "phase-owned implementation_goals narrative is duplicated" in item
        for item in repeated.diagnostics
    )


def test_every_phase_goal_requires_source_grounded_non_vacuous_assurance() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "durable-counter")

    class GoalRunner(FixturePlannerRunner):
        def __init__(self, replacement: str):
            self.replacement = replacement

        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier == "base":
                replacements = _replace_roadmap_string(
                    response["roadmap"],
                    "Use Verus to prove arithmetic safety, state refinement, and restart preservation.",
                    self.replacement,
                )
                assert replacements == 1
            return response

    formal = evaluate_scenario(
        REPOSITORY,
        seed,
        case.base,
        GoalRunner("Prove acknowledged updates remain visible after process restart."),
    )
    grounded_without_lexical_intent = evaluate_scenario(
        REPOSITORY,
        seed,
        case.base,
        GoalRunner("Confirm acknowledged updates remain visible after process restart."),
    )
    boilerplate = evaluate_scenario(
        REPOSITORY,
        seed,
        case.base,
        GoalRunner("Perform verification."),
    )

    assert formal.mechanical
    assert formal.metrics["verification_policy"] == 1.0
    assert grounded_without_lexical_intent.mechanical
    assert grounded_without_lexical_intent.metrics["verification_policy"] == 1.0
    assert not any(
        "verification or qualification goal" in item
        for item in grounded_without_lexical_intent.diagnostics
    )
    assert boilerplate.mechanical
    assert boilerplate.metrics["verification_policy"] < 1.0
    assert any("verification or qualification goal" in item for item in boilerplate.diagnostics)


@pytest.mark.parametrize(
    "before,after,diagnostic,mechanically_admissible",
    [
        (
            (
                "Production logic has meaningful contracts, component refinement, complete "
                "coverage, and exact-tree composition; trusted effects remain narrow and "
                "policy-free."
            ),
            "Production verification is recommended.",
            "exactly one source-free oxide-verification-policy",
            False,
        ),
        (
            'applicable_global_invariants = ["oxide-verification-policy"]',
            "applicable_global_invariants = []",
            "does not apply oxide-verification-policy",
            False,
        ),
        (
            "Use Verus to prove arithmetic safety, state refinement, and restart preservation.",
            "Perform verification.",
            "verification or qualification goal",
            True,
        ),
    ],
)
def test_verification_policy_failure_is_a_hard_score_guard(
    before: str, after: str, diagnostic: str, mechanically_admissible: bool
) -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class PolicyMutationRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if before == 'applicable_global_invariants = ["oxide-verification-policy"]':
                stage = _roadmap_stage(response["roadmap"], "counter-core")
                assert stage["applicable_global_invariants"] == ["oxide-verification-policy"]
                stage["applicable_global_invariants"] = []
            else:
                replacements = _replace_roadmap_string(response["roadmap"], before, after)
                assert replacements == 1
            return response

    harness = EvaluationHarness(REPOSITORY, seed, cases, PolicyMutationRunner())
    report = harness.evaluate(seed, harness.cases["durable-counter"])

    if mechanically_admissible:
        assert report.base.mechanical and report.variant.mechanical
        assert report.base.metrics["verification_policy"] < 1.0
        assert report.score == 0.20
    else:
        assert not report.base.mechanical
        assert report.score == 0.0
    assert any(diagnostic in item for item in report.diagnostics)


def test_goal_type_appropriateness_is_not_lexically_hard_capped() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()
    before = (
        "Deterministically validate sealed campaign identities and report schemas, enforce that "
        "runners have no direct authority path, and treat all capacity and fault outcomes as "
        "empirical evidence rather than proof."
    )

    class FakeProofRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            replacements = _replace_roadmap_string(
                response["roadmap"],
                before,
                "Use Verus to prove capacity and recovery behavior.",
            )
            assert replacements == 1
            return response

    harness = EvaluationHarness(REPOSITORY, seed, cases, FakeProofRunner())
    report = harness.evaluate(seed, harness.cases["collaborative-document"])

    assert report.base.mechanical and report.variant.mechanical
    assert report.base.metrics["verification_policy"] == 1.0
    assert report.variant.metrics["verification_policy"] == 1.0
    assert not any("verification or qualification goal" in item for item in report.diagnostics)


def test_study_plan_like_evidence_phase_cannot_omit_universal_policy() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class PolicyOmissionRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            stage = _roadmap_stage(response["roadmap"], "empirical-qualification")
            assert "evidence" in " ".join(stage["verification_goals"])
            assert "oxide-verification-policy" in stage["applicable_global_invariants"]
            stage["applicable_global_invariants"].remove("oxide-verification-policy")
            return response

    harness = EvaluationHarness(REPOSITORY, seed, cases, PolicyOmissionRunner())
    report = harness.evaluate(seed, harness.cases["collaborative-document"])

    assert not report.base.mechanical
    assert report.score == 0.0
    assert any("does not apply oxide-verification-policy" in item for item in report.diagnostics)


def test_variant_localizes_ambiguity_without_propagating_readiness() -> None:
    seed, harness = _harness()
    case = harness.cases["agent-message-board"]

    report = harness.evaluate(seed, case)

    assert report.variant.response["ready_for_approval"] is True
    assert report.variant.response["unresolved"] == []
    assert report.variant.roadmap is not None
    assert report.variant.roadmap["status"] == "ready"
    assert report.variant.requirement_readiness["claim-winner"] == ("blocked",)
    assert report.variant.requirement_readiness["semantic-nonauthority"] == ("ready",)
    assert report.metamorphic_score == 1.0


def test_localized_block_allows_affected_phase_split_and_rename() -> None:
    seed, harness = _harness()
    case = harness.cases["agent-message-board"]

    baseline = harness.evaluate(seed, case)

    assert baseline.base.requirement_stages["claim-winner"] == ("claim-arbitration",)
    assert baseline.variant.requirement_stages["claim-winner"] == ("claim-arbitration",)
    assert _roadmap_stage(baseline.variant.roadmap, "typed-coordination")["readiness"] == "ready"
    assert _roadmap_stage(baseline.variant.roadmap, "claim-arbitration")["readiness"] == "blocked"
    assert baseline.metamorphic_score == 1.0

    class RenamedAffectedPhaseRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier == "variant":
                replacements = _replace_roadmap_string(
                    response["roadmap"],
                    "claim-arbitration",
                    "claim-winner-resolution",
                )
                assert replacements >= 2
            return response

    renamed = EvaluationHarness(
        REPOSITORY,
        seed,
        load_cases(),
        RenamedAffectedPhaseRunner(),
    ).evaluate(seed, case)

    assert renamed.base.mechanical and renamed.variant.mechanical
    assert renamed.variant.requirement_stages["claim-winner"] == ("claim-winner-resolution",)
    assert renamed.metamorphic_score == 1.0
    assert not any("metamorphic variant" in item for item in renamed.diagnostics)


def test_affected_contract_unit_can_split_without_structural_penalty() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")
    base = evaluate_scenario(REPOSITORY, seed, case.base, FixturePlannerRunner())
    variant = evaluate_scenario(REPOSITORY, seed, case.variant, FixturePlannerRunner())

    assert variant.roadmap is not None
    affected = _roadmap_stage(variant.roadmap, "claim-arbitration")
    affected["id"] = "claim-winner-contract"
    for stage in variant.roadmap["stages"]:
        stage["dependencies"] = [
            "claim-winner-contract" if item == "claim-arbitration" else item
            for item in stage["dependencies"]
        ]
    split = copy.deepcopy(affected)
    split["id"] = "claim-winner-proof"
    variant.roadmap["stages"].append(split)

    score, diagnostics = _localized_structure_score(case, base, variant)

    assert score == 1.0
    assert diagnostics == []


@pytest.mark.parametrize(
    "mutation,diagnostic",
    [
        ("rename", "renamed unaffected phases"),
        ("dependency", "changed dependencies between unaffected phases"),
        ("invariant", "changed invariant placement for unchanged sources"),
    ],
)
def test_affected_stage_residual_group_is_not_exempt_from_structure(
    mutation: str, diagnostic: str
) -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")
    base = evaluate_scenario(REPOSITORY, seed, case.base, FixturePlannerRunner())
    variant = evaluate_scenario(REPOSITORY, seed, case.variant, FixturePlannerRunner())

    for report in (base, variant):
        assert report.roadmap is not None
        roadmap = report.roadmap
        residual = _roadmap_stage(roadmap, "typed-coordination")
        affected = _roadmap_stage(roadmap, "claim-arbitration")
        residual["source_specifications"].extend(affected["source_specifications"])
        roadmap["stages"].remove(affected)
        for stage in roadmap["stages"]:
            rewritten: list[str] = []
            for dependency in stage["dependencies"]:
                dependency = (
                    "typed-coordination" if dependency == "claim-arbitration" else dependency
                )
                if dependency not in rewritten:
                    rewritten.append(dependency)
            stage["dependencies"] = rewritten

    assert variant.roadmap is not None
    if mutation == "rename":
        replacements = _replace_roadmap_string(
            variant.roadmap,
            "typed-coordination",
            "coordination-projection",
        )
        assert replacements >= 2
    elif mutation == "dependency":
        _roadmap_stage(variant.roadmap, "typed-coordination")["dependencies"].append(
            "trusted-effects-and-capabilities"
        )
    else:
        residual = _roadmap_stage(variant.roadmap, "typed-coordination")
        source = next(
            item
            for item in residual["source_specifications"]
            if item["anchor"] == "7.6 Projection replay"
        )
        variant.roadmap["global_invariants"].append(
            {
                "id": "projection-replay",
                "statement": "Replaying the same canonical record sequence produces the same coordination projection.",
                "sources": [copy.deepcopy(source)],
            }
        )
        residual["applicable_global_invariants"].append("projection-replay")

    score, diagnostics = _localized_structure_score(case, base, variant)

    assert score == 0.0
    assert any(diagnostic in item for item in diagnostics)


def test_affected_only_invariant_source_does_not_exempt_unaffected_applications() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")
    base = evaluate_scenario(REPOSITORY, seed, case.base, FixturePlannerRunner())
    variant = evaluate_scenario(REPOSITORY, seed, case.variant, FixturePlannerRunner())

    for report, application in (
        (base, "durable-delivery"),
        (variant, "bounded-context-recovery"),
    ):
        assert report.roadmap is not None
        affected_source = copy.deepcopy(
            _roadmap_stage(report.roadmap, "claim-arbitration")["source_specifications"][0]
        )
        report.roadmap["global_invariants"].append(
            {
                "id": "claim-order-is-stable",
                "statement": "Claim arbitration follows the canonical board order.",
                "sources": [affected_source],
            }
        )
        _roadmap_stage(report.roadmap, application)["applicable_global_invariants"].append(
            "claim-order-is-stable"
        )

    score, diagnostics = _localized_structure_score(case, base, variant)

    assert score == 0.0
    assert any("changed invariant placement for unchanged sources" in item for item in diagnostics)


def test_affected_only_contract_unit_keeps_incident_dependencies_observable() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")
    base = evaluate_scenario(REPOSITORY, seed, case.base, FixturePlannerRunner())
    variant = evaluate_scenario(REPOSITORY, seed, case.variant, FixturePlannerRunner())

    assert base.roadmap is not None and variant.roadmap is not None
    base_consumer = _roadmap_stage(base.roadmap, "bounded-context-recovery")
    variant_consumer = _roadmap_stage(variant.roadmap, "bounded-context-recovery")
    assert "claim-arbitration" in base_consumer["dependencies"]
    assert "claim-arbitration" in variant_consumer["dependencies"]
    variant_consumer["dependencies"].remove("claim-arbitration")

    score, diagnostics = _localized_structure_score(case, base, variant)

    assert score == 0.0
    assert any("changed dependencies between unaffected phases" in item for item in diagnostics)


def test_variant_only_contract_unit_keeps_incident_dependencies_observable() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")
    base = evaluate_scenario(REPOSITORY, seed, case.base, FixturePlannerRunner())
    variant = evaluate_scenario(REPOSITORY, seed, case.variant, FixturePlannerRunner())

    assert variant.roadmap is not None
    affected = _roadmap_stage(variant.roadmap, "claim-arbitration")
    delta_source = next(
        source
        for source in affected["source_specifications"]
        if source["path"].endswith("/CLAIM-CONFLICT.md")
    )
    affected["source_specifications"].remove(delta_source)
    delta_only = copy.deepcopy(affected)
    delta_only["id"] = "claim-conflict-override"
    delta_only["dependencies"] = ["durable-delivery"]
    delta_only["source_specifications"] = [delta_source]
    variant.roadmap["stages"].append(delta_only)

    score, diagnostics = _localized_structure_score(case, base, variant)

    assert score == 0.0
    assert any("changed dependencies between unaffected phases" in item for item in diagnostics)


def test_enclosing_affected_citation_does_not_mask_unchanged_structure() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")
    base = evaluate_scenario(REPOSITORY, seed, case.base, FixturePlannerRunner())
    variant = evaluate_scenario(REPOSITORY, seed, case.variant, FixturePlannerRunner())
    claim_section = """\
A Claim competes for exclusive ownership of one claim key and claim generation.

Its payload contains claim key, claim generation, subject task keys, requested owner principal, requested owner session generation, and expected predecessor token.

Claim generation starts at zero.

For one claim key and generation, the valid Claim with the lowest BoardSeq is the winner.

All later valid Claims for that same key and generation remain evidence but are losers.

The winner is unique because BoardSeq is unique.

The requested owner session must be current at the Claim's BoardSeq.

A Claim does not expire by wall-clock time.
""".strip()

    for report in (base, variant):
        assert report.roadmap is not None
        source = next(
            item
            for item in _roadmap_stage(report.roadmap, "claim-arbitration")["source_specifications"]
            if item["anchor"] == "4.4 Claim"
        )
        source["requirement"] = claim_section
    assert variant.roadmap is not None
    replacements = _replace_roadmap_string(
        variant.roadmap,
        "claim-arbitration",
        "claim-winner-resolution",
    )
    assert replacements >= 2

    score, diagnostics = _localized_structure_score(case, base, variant)

    assert score == 0.0
    assert any("renamed unaffected phases" in item for item in diagnostics)


@pytest.mark.parametrize(
    "mutation,diagnostic",
    [
        ("source", "changed unaffected source coverage"),
        ("owner", "repartitioned unchanged source ownership"),
        ("rename", "renamed unaffected phases"),
        ("dependency", "changed dependencies between unaffected phases"),
        ("invariant", "changed invariant placement for unchanged sources"),
        ("narrative", "rewrote unchanged phase narrative"),
    ],
)
def test_localized_block_penalizes_unaffected_structural_drift(
    mutation: str, diagnostic: str
) -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class StructuralDriftRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier != "variant":
                return response
            roadmap = response["roadmap"]
            foundation = _roadmap_stage(roadmap, "formal-board-foundation")
            source = next(
                item
                for item in foundation["source_specifications"]
                if item["anchor"] == "3.3 State invariants"
            )
            if mutation == "source":
                foundation["source_specifications"].remove(source)
            elif mutation == "owner":
                foundation["source_specifications"].remove(source)
                _roadmap_stage(roadmap, "trusted-effects-and-capabilities")[
                    "source_specifications"
                ].append(source)
            elif mutation == "rename":
                replacements = _replace_roadmap_string(
                    roadmap,
                    "durable-delivery",
                    "subscription-delivery",
                )
                assert replacements >= 2
            elif mutation == "dependency":
                capacity = _roadmap_stage(roadmap, "capacity-and-agent-utility")
                capacity["dependencies"].append("durable-delivery")
            elif mutation == "invariant":
                roadmap["global_invariants"].append(
                    {
                        "id": "record-id-uniqueness",
                        "statement": "Every RecordId maps to exactly one canonical record.",
                        "sources": [copy.deepcopy(source)],
                    }
                )
                for phase_id in ("formal-board-foundation", "atomic-global-publication"):
                    _roadmap_stage(roadmap, phase_id)["applicable_global_invariants"].append(
                        "record-id-uniqueness"
                    )
            else:
                _roadmap_stage(roadmap, "durable-delivery")["outcome"] = (
                    "Subscriptions use a newly rewritten unrelated delivery outcome."
                )
            return response

    harness = EvaluationHarness(REPOSITORY, seed, cases, StructuralDriftRunner())
    report = harness.evaluate(seed, harness.cases["agent-message-board"])

    assert report.base.mechanical and report.variant.mechanical
    assert report.deterministic_score == 1.0
    assert report.metamorphic_score < 1.0
    if mutation == "rename":
        assert report.metamorphic_score == 0.5
    assert any(diagnostic in item for item in report.diagnostics)


def test_localized_block_penalizes_unrepresented_source_readiness_drift() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class ReadinessDriftRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier == "variant":
                response["roadmap"] = _replace_phase_readiness(
                    response["roadmap"],
                    "verified-foundations",
                    "ready",
                    "blocked",
                )
            return response

    harness = EvaluationHarness(REPOSITORY, seed, cases, ReadinessDriftRunner())
    report = harness.evaluate(seed, harness.cases["transactional-reservation"])

    assert report.base.mechanical and report.variant.mechanical
    assert not any(
        "verified-foundations" in stages for stages in report.variant.requirement_stages.values()
    )
    assert report.deterministic_score == 1.0
    assert report.metamorphic_score < 1.0
    assert any(
        "changed readiness of unchanged source obligations" in item for item in report.diagnostics
    )


def test_localized_source_identity_is_relative_to_each_scenario_root() -> None:
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")
    fixture = FixturePlannerRunner()
    base = fixture.run("prompt", case.base)["roadmap"]
    variant = fixture.run("prompt", case.variant)["roadmap"]
    base_source = next(
        item
        for item in _roadmap_stage(base, "formal-board-foundation")["source_specifications"]
        if item["anchor"] == "3.3 State invariants"
    )
    variant_source = next(
        item
        for item in _roadmap_stage(variant, "formal-board-foundation")["source_specifications"]
        if item["anchor"] == "3.3 State invariants"
    )

    assert base_source["path"] != variant_source["path"]
    assert _relative_source_identity(base_source, case.base) == _relative_source_identity(
        variant_source, case.variant
    )


def test_evaluator_rejects_a_foreign_roadmap_specification_root() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "durable-counter")

    class ForeignRootRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            response["roadmap"]["specification_root"] = "eval/examples/private-notes/base/specs"
            return response

    report = evaluate_scenario(REPOSITORY, seed, case.base, ForeignRootRunner())

    assert not report.mechanical
    assert report.score == 0.05
    assert report.metrics == {"response_shape": 1.0, "mechanical_validity": 0.0}
    assert report.diagnostics == [
        (
            "roadmap qualification failed: specification_root does not match the evaluated "
            "scenario: expected 'eval/examples/durable-counter/base/specs', received "
            "'eval/examples/private-notes/base/specs'"
        )
    ]


@pytest.mark.parametrize(
    "mutation,diagnostic,expected_score",
    [
        ("rename", "renamed unaffected phases", 0.5),
        ("dependency", "changed dependencies between unaffected phases", 1.0 / 3.0),
        ("narrative", "rewrote unchanged phase narrative", 0.5),
    ],
)
def test_equivalent_formatting_penalizes_unrelated_structural_drift(
    mutation: str, diagnostic: str, expected_score: float
) -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class EquivalentDriftRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier != "variant":
                return response
            roadmap = response["roadmap"]
            if mutation == "rename":
                replacements = _replace_roadmap_string(
                    roadmap,
                    "http-adapter",
                    "http-transport",
                )
                assert replacements == 1
            elif mutation == "dependency":
                _roadmap_stage(roadmap, "http-adapter")["dependencies"] = []
            else:
                _roadmap_stage(roadmap, "http-adapter")["outcome"] = (
                    "A rewritten HTTP outcome replaces the unchanged deferred contract."
                )
            return response

    harness = EvaluationHarness(REPOSITORY, seed, cases, EquivalentDriftRunner())
    report = harness.evaluate(seed, harness.cases["durable-counter"])

    assert report.base.mechanical and report.variant.mechanical
    assert report.metamorphic_score == pytest.approx(expected_score)
    assert any(diagnostic in item for item in report.diagnostics)


def test_equivalent_narrative_comparison_ignores_cosmetic_markdown() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class CosmeticNarrativeRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier == "variant":
                _roadmap_stage(response["roadmap"], "counter-core")["included_scope"][0] = (
                    "**Signed** updates"
                )
            return response

    harness = EvaluationHarness(REPOSITORY, seed, cases, CosmeticNarrativeRunner())
    report = harness.evaluate(seed, harness.cases["durable-counter"])

    assert report.base.mechanical and report.variant.mechanical
    assert report.metamorphic_score == 1.0
    assert not any("metamorphic variant" in item for item in report.diagnostics)


def test_approval_ready_response_requires_empty_top_level_unresolved() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "durable-counter")

    class StrayUnresolvedRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            response["unresolved"] = ["A phase-local deferral was incorrectly promoted."]
            return response

    report = evaluate_scenario(REPOSITORY, seed, case.base, StrayUnresolvedRunner())

    assert report.mechanical
    assert report.metrics["approval_alignment"] < 1.0
    assert any("approval/readiness flags" in item for item in report.diagnostics)


def test_dependency_readiness_propagation_loses_metamorphic_score() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class PropagatingRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier == "variant":
                response["roadmap"] = _replace_phase_readiness(
                    response["roadmap"],
                    "bounded-context-recovery",
                    "ready",
                    "blocked",
                )
            return response

    harness = EvaluationHarness(REPOSITORY, seed, cases, PropagatingRunner())
    report = harness.evaluate(seed, harness.cases["agent-message-board"])

    assert report.metamorphic_score < 1.0
    assert any(
        "unaffected requirement 'semantic-nonauthority' changed readiness" in item
        for item in report.diagnostics
    )


def test_fatal_baseline_failure_skips_remaining_model_turns() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class FatalRunner:
        calls = 0

        def run(self, prompt, scenario):
            del prompt, scenario
            self.calls += 1
            return {
                "message": "proposal could not be qualified",
                "ready_for_approval": False,
                "complete_specification_corpus": True,
                "faithful_to_specifications": True,
                "unresolved": [],
                "roadmap": {},
            }

    class RecordingJudge:
        calls = 0

        def score(self, case, base_response, variant_response):
            del case, base_response, variant_response
            self.calls += 1
            return 1.0, {}

    runner = FatalRunner()
    judge = RecordingJudge()
    harness = EvaluationHarness(REPOSITORY, seed, cases, runner, judge, replicates=3)

    report = harness.evaluate(seed, harness.cases["durable-counter"])

    assert runner.calls == 1
    assert judge.calls == 0
    assert report.score == 0.0
    assert report.variant.metrics == {"not_run": 1.0}
    assert any(
        "repeat runs and quality judging were skipped" in item for item in report.diagnostics
    )


def test_transient_planning_failure_is_not_scored_or_cached_by_gepa(tmp_path: Path) -> None:
    pytest.importorskip("gepa")
    seed = SEED_PATH.read_text(encoding="utf-8")
    case = next(item for item in load_cases() if item.identifier == "durable-counter")

    class FailingRunner:
        calls = 0

        def run(self, prompt, scenario):
            del prompt, scenario
            self.calls += 1
            raise PlanningInfrastructureError("temporary planning outage")

    runner = FailingRunner()
    harness = EvaluationHarness(REPOSITORY, seed, [case], runner, replicates=2)
    run_directory = tmp_path / "gepa"

    with pytest.raises(PlanningInfrastructureError, match="temporary planning outage"):
        optimize_prompt(
            seed,
            harness,
            proposer=lambda candidate, _dataset, components: {
                component: candidate[component] for component in components
            },
            run_directory=run_directory,
            max_metric_calls=1,
            max_candidate_proposals=0,
        )

    assert runner.calls == 1
    assert not list((run_directory / "fitness_cache").glob("*.pkl"))


def test_evaluation_manifest_fails_closed_on_run_identity_drift(tmp_path: Path) -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()
    harness = EvaluationHarness(REPOSITORY, seed, cases, FixturePlannerRunner())
    manifest = harness.bind_optimizer(None)
    run_directory = tmp_path / "run"

    manifest_path = qualify_run_directory(run_directory, manifest)
    qualify_run_directory(run_directory, manifest)

    stored = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert stored["fingerprint"] == harness.evaluation_fingerprint
    assert stored["execution"]["replicates"] == 2
    assert stored["weights"]["judge"] == JUDGE_WEIGHT
    changed = build_manifest(
        REPOSITORY,
        cases=cases,
        seed_template=seed,
        runner=CodexPlannerRunner(REPOSITORY, timeout_seconds=17.0),
        judge=None,
        proposer=None,
        replicates=2,
        weights=harness.weights(),
    )
    with pytest.raises(EvaluationIdentityError, match="use a new output directory"):
        qualify_run_directory(run_directory, changed)


def test_evaluation_manifest_rejects_unidentified_existing_state(tmp_path: Path) -> None:
    _seed, harness = _harness()
    run_directory = tmp_path / "old-run"
    run_directory.mkdir()
    (run_directory / "gepa_state.bin").write_bytes(b"unidentified")

    with pytest.raises(EvaluationIdentityError, match="no identity manifest"):
        qualify_run_directory(run_directory, harness.bind_optimizer(None))


def test_corpus_and_rubric_bytes_change_the_evaluation_fingerprint(tmp_path: Path) -> None:
    case_directory = tmp_path / "example"
    (case_directory / "example.toml").parent.mkdir(parents=True)
    (case_directory / "example.toml").write_text("id = 'fixture'\n", encoding="utf-8")
    scenarios = []
    for identifier in ("base", "variant"):
        directory = case_directory / identifier
        (directory / "specs").mkdir(parents=True)
        (directory / "rubric.toml").write_text("expected_approval = true\n", encoding="utf-8")
        (directory / "model-free-output.md").write_text("# Roadmap\n", encoding="utf-8")
        (directory / "specs" / "PRODUCT.md").write_text("# Product\nA.\n", encoding="utf-8")
        scenarios.append(SimpleNamespace(directory=directory))
    case = SimpleNamespace(identifier="fixture", base=scenarios[0], variant=scenarios[1])
    arguments = {
        "repository": REPOSITORY,
        "cases": [case],
        "seed_template": "{{ specification_bundle }}",
        "runner": FixturePlannerRunner(),
        "judge": None,
        "proposer": None,
        "replicates": 2,
        "weights": {"deterministic": 1.0},
    }

    first = build_manifest(**arguments)
    (case_directory / "base" / "specs" / "PRODUCT.md").write_text(
        "# Product\nB.\n", encoding="utf-8"
    )
    second = build_manifest(**arguments)

    assert first["fingerprint"] != second["fingerprint"]
    assert first["corpus_and_rubrics"] != second["corpus_and_rubrics"]


def test_dataset_binds_gepa_cache_keys_to_the_evaluation_fingerprint() -> None:
    seed, harness = _harness()
    example = harness.dataset()[0]

    assert example["evaluation_fingerprint"] == harness.evaluation_fingerprint
    stale = dict(example, evaluation_fingerprint="0" * 64)
    score, side_info = harness({"planning_prompt": seed}, stale)

    assert score == 0.0
    assert side_info["diagnostics"] == ["dataset evaluation fingerprint is stale or foreign"]


def test_codex_planner_judge_and_proposer_retry_one_infrastructure_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval import runners

    class FlakySession:
        calls = 0

        def __init__(self, *args, **kwargs):
            del args, kwargs

        def start(self, prompt, schema):
            del prompt
            self.__class__.calls += 1
            if self.__class__.calls % 2:
                raise PlanningInfrastructureError("temporary")
            properties = schema["properties"]
            if "faithfulness" in properties:
                return {
                    "faithfulness": 4,
                    "coverage": 4,
                    "decomposition": 4,
                    "readability": 4,
                    "reason": "qualified",
                }
            if "planning_prompt" in properties:
                return {"message": "improved", "planning_prompt": "replacement"}
            return {"message": "planned"}

    monkeypatch.setattr(runners, "CodexSessionAgent", FlakySession)
    case = load_cases()[0]

    assert CodexPlannerRunner(REPOSITORY).run("prompt", case.base) == {"message": "planned"}
    score, _details = CodexQualityJudge(REPOSITORY).score(case, {}, {})
    assert score == 1.0
    proposal = CodexPromptProposer(REPOSITORY, "objective")(
        {"planning_prompt": "current"}, {}, ["planning_prompt"]
    )
    assert proposal == {"planning_prompt": "replacement"}
    assert FlakySession.calls == 6


def test_codex_turn_propagates_after_one_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    from eval import runners

    class FailingSession:
        calls = 0

        def __init__(self, *args, **kwargs):
            del args, kwargs

        def start(self, prompt, schema):
            del prompt, schema
            self.__class__.calls += 1
            raise PlanningInfrastructureError("still unavailable")

    monkeypatch.setattr(runners, "CodexSessionAgent", FailingSession)
    with pytest.raises(PlanningInfrastructureError, match="still unavailable"):
        CodexPlannerRunner(REPOSITORY).run("prompt", load_cases()[0].base)
    assert FailingSession.calls == 2


def test_candidate_cannot_drop_production_jinja_inputs() -> None:
    seed, harness = _harness()
    candidate = seed.replace("{{ verification_policy }}", "")

    score, side_info = harness(
        {"planning_prompt": candidate},
        {
            "case_id": "durable-counter",
            "evaluation_fingerprint": harness.evaluation_fingerprint,
        },
    )

    assert score == 0.0
    assert "template variable contract changed" in side_info["diagnostics"][0]


def test_malformed_candidate_fails_before_a_planning_turn() -> None:
    seed, harness = _harness()

    score, side_info = harness(
        {"planning_prompt": seed + "\n{{ missing_eval_value }}"},
        {
            "case_id": "retry-queue",
            "evaluation_fingerprint": harness.evaluation_fingerprint,
        },
    )

    assert score == 0.0
    assert "added=['missing_eval_value']" in side_info["diagnostics"][0]


def test_ambiguous_requirement_cannot_be_silently_marked_ready() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class RecklessRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier == "variant":
                response["roadmap"] = _replace_phase_readiness(
                    response["roadmap"],
                    "retry-policy",
                    "blocked",
                    "ready",
                )
            return response

    harness = EvaluationHarness(REPOSITORY, seed, cases, RecklessRunner())
    report = harness.evaluate(seed, harness.cases["retry-queue"])

    assert report.score < 1.0
    assert report.metamorphic_score < 1.0
    assert any("did not move from ready to non-ready" in item for item in report.diagnostics)


def test_gepa_executes_the_real_evaluator_contract_model_free(tmp_path: Path) -> None:
    pytest.importorskip("gepa")
    seed, harness = _harness()

    def unchanged(candidate, reflective_dataset, components_to_update):
        del reflective_dataset
        return {component: candidate[component] for component in components_to_update}

    result = optimize_prompt(
        seed,
        harness,
        proposer=unchanged,
        run_directory=tmp_path / "gepa",
        max_metric_calls=len(harness.cases),
        max_candidate_proposals=0,
    )

    assert result.best_candidate == {"planning_prompt": seed}
    assert result.val_aggregate_scores[result.best_idx] == 1.0
    assert result.total_metric_calls == len(harness.cases)
