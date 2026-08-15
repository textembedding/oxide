from __future__ import annotations

import re
from pathlib import Path

import pytest

from eval.cases import load_cases
from eval.gepa_harness import optimize_prompt
from eval.runners import FixturePlannerRunner, _judge_source_bundle
from eval.scoring import EvaluationHarness

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


def test_candidate_cannot_drop_production_jinja_inputs() -> None:
    seed, harness = _harness()
    candidate = seed.replace("{{ verification_policy }}", "")

    score, side_info = harness(
        {"planning_prompt": candidate},
        {"case_id": "durable-counter"},
    )

    assert score == 0.0
    assert "template variable contract changed" in side_info["diagnostics"][0]


def test_malformed_candidate_fails_before_a_planning_turn() -> None:
    seed, harness = _harness()

    score, side_info = harness(
        {"planning_prompt": seed + "\n{{ missing_eval_value }}"},
        {"case_id": "retry-queue"},
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
