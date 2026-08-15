from __future__ import annotations

from pathlib import Path

import pytest

from eval.cases import load_cases
from eval.gepa_harness import optimize_prompt
from eval.runners import FixturePlannerRunner
from eval.scoring import EvaluationHarness

REPOSITORY = Path(__file__).resolve().parents[2]
SEED_PATH = REPOSITORY / "src" / "oxide" / "prompts" / "planning.md.j2"


def _harness() -> tuple[str, EvaluationHarness]:
    seed = SEED_PATH.read_text(encoding="utf-8")
    cases = load_cases()
    return seed, EvaluationHarness(REPOSITORY, seed, cases, FixturePlannerRunner())


def test_three_cases_cover_distinct_metamorphic_relations() -> None:
    cases = load_cases()

    assert [case.identifier for case in cases] == [
        "durable-counter",
        "private-notes",
        "retry-queue",
    ]
    assert [case.relation for case in cases] == ["equivalent", "must-block", "must-block"]


def test_fixture_outputs_score_every_deterministic_and_metamorphic_objective() -> None:
    seed, harness = _harness()

    reports = [harness.evaluate(seed, case) for case in harness.cases.values()]

    assert [report.score for report in reports] == [1.0, 1.0, 1.0]
    assert all(not report.diagnostics for report in reports)
    assert all(report.base.mechanical and report.variant.mechanical for report in reports)


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
        max_metric_calls=3,
        max_candidate_proposals=0,
    )

    assert result.best_candidate == {"planning_prompt": seed}
    assert result.val_aggregate_scores[result.best_idx] == 1.0
    assert result.total_metric_calls == 3
