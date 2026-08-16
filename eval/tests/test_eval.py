from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from eval.cases import load_cases
from eval.gepa_harness import optimize_prompt
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
    evaluate_scenario,
)
from oxide.planning import PlanningInfrastructureError

REPOSITORY = Path(__file__).resolve().parents[2]
SEED_PATH = REPOSITORY / "src" / "oxide" / "prompts" / "planning.md.j2"


def _harness() -> tuple[str, EvaluationHarness]:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()
    return seed, EvaluationHarness(REPOSITORY, seed, cases, FixturePlannerRunner())


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
    assert all(relations[identifier] == "must-block" for identifier in BENCHMARK_CASES)


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


def test_judge_sees_complete_base_corpus_once_plus_only_variant_delta() -> None:
    case = next(item for item in load_cases() if item.identifier == "agent-message-board")

    bundle = _judge_source_bundle(case)

    assert bundle.count("# Agent Message Board Product Specification") == 1
    assert bundle.count("# Agent Message Board Development Specification") == 1
    assert bundle.count("# Agent Message Board Research Specification") == 1
    assert "VARIANT SOURCE specs/CLAIM-CONFLICT.md" in bundle


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
                    response["roadmap_markdown"] = response["roadmap_markdown"].replace(
                        "A durable checked counter is available through its core API.",
                        "The core API exposes a durable counter with checked updates.",
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
                "roadmap_markdown": "# Roadmap\n",
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


def test_silent_contractibility_inference_loses_score() -> None:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()

    class RecklessRunner(FixturePlannerRunner):
        def run(self, prompt, scenario):
            response = super().run(prompt, scenario)
            if scenario.identifier == "variant":
                response["ready_for_approval"] = True
                response["unresolved"] = []
            return response

    harness = EvaluationHarness(REPOSITORY, seed, cases, RecklessRunner())
    report = harness.evaluate(seed, harness.cases["retry-queue"])

    assert report.score < 1.0
    assert report.metamorphic_score < 1.0
    assert any("silently treated as contractible" in item for item in report.diagnostics)


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
