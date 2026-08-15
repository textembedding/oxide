"""Command-line entry points for scoring and optimizing the planning prompt."""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any

from .cases import load_cases
from .gepa_harness import OBJECTIVE, optimize_prompt, write_result
from .runners import (
    CodexPlannerRunner,
    CodexPromptProposer,
    CodexQualityJudge,
    FixturePlannerRunner,
)
from .scoring import EvaluationHarness

REPOSITORY = Path(__file__).resolve().parents[1]
DEFAULT_CANDIDATE = REPOSITORY / "src" / "oxide" / "prompts" / "planning.md.j2"


def _harness(arguments: argparse.Namespace) -> tuple[str, EvaluationHarness]:
    candidate = Path(arguments.candidate).resolve().read_text(encoding="utf-8")
    cases = load_cases()
    if arguments.runner == "fixture":
        runner = FixturePlannerRunner()
    else:
        runner = CodexPlannerRunner(
            REPOSITORY,
            model=arguments.model,
            reasoning_effort=arguments.reasoning_effort,
            timeout_seconds=arguments.timeout,
        )
    judge = (
        CodexQualityJudge(
            REPOSITORY,
            model=arguments.model,
            reasoning_effort=arguments.reasoning_effort,
            timeout_seconds=arguments.timeout,
        )
        if arguments.judge == "codex"
        else None
    )
    return candidate, EvaluationHarness(REPOSITORY, candidate, cases, runner, judge)


def _score(arguments: argparse.Namespace) -> int:
    candidate, harness = _harness(arguments)
    reports = [harness.evaluate(candidate, case) for case in harness.cases.values()]
    payload = {
        "aggregate_score": sum(report.score for report in reports) / len(reports),
        "cases": [report.side_info() for report in reports],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if all(report.score >= arguments.minimum_score for report in reports) else 1


def _noop_proposer(
    candidate: dict[str, str],
    reflective_dataset: Any,
    components_to_update: list[str],
) -> dict[str, str]:
    del reflective_dataset
    return {component: candidate[component] for component in components_to_update}


def _optimize(arguments: argparse.Namespace) -> int:
    candidate, harness = _harness(arguments)
    proposer = (
        CodexPromptProposer(
            REPOSITORY,
            OBJECTIVE,
            model=arguments.model,
            reasoning_effort=arguments.reasoning_effort,
            timeout_seconds=arguments.timeout,
        )
        if arguments.proposer == "codex"
        else _noop_proposer
    )
    result = optimize_prompt(
        candidate,
        harness,
        proposer=proposer,
        run_directory=Path(arguments.output).resolve(),
        max_metric_calls=arguments.max_metric_calls,
        max_candidate_proposals=arguments.max_proposals,
        seed=arguments.seed,
    )
    prompt, receipt = write_result(result, Path(arguments.output).resolve())
    print(f"best score: {result.val_aggregate_scores[result.best_idx]:.4f}")
    print(f"prompt: {prompt}")
    print(f"receipt: {receipt}")
    return 0


def _smoke(arguments: argparse.Namespace) -> int:
    arguments.runner = "fixture"
    arguments.judge = "none"
    candidate, harness = _harness(arguments)
    reports = [harness.evaluate(candidate, case) for case in harness.cases.values()]
    if not reports or any(report.score != 1.0 for report in reports):
        print(json.dumps([report.side_info() for report in reports], indent=2))
        return 1
    with tempfile.TemporaryDirectory(prefix="oxide-gepa-smoke-") as temporary:
        result = optimize_prompt(
            candidate,
            harness,
            proposer=_noop_proposer,
            run_directory=Path(temporary),
            max_metric_calls=len(reports),
            max_candidate_proposals=0,
        )
        if result.best_candidate != {"planning_prompt": candidate}:
            return 1
    print(f"{len(reports)} model-free examples passed; GEPA seed evaluation completed")
    return 0


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--candidate", default=str(DEFAULT_CANDIDATE))
    parser.add_argument("--runner", choices=("fixture", "codex"), default="codex")
    parser.add_argument("--judge", choices=("none", "codex"), default="none")
    parser.add_argument("--model", default="gpt-5.6-sol")
    parser.add_argument("--reasoning-effort", default="max")
    parser.add_argument("--timeout", type=float, default=900.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m eval")
    commands = parser.add_subparsers(dest="command", required=True)
    score = commands.add_parser("score", help="score one planning prompt")
    _common(score)
    score.add_argument("--minimum-score", type=float, default=0.0)
    score.set_defaults(handler=_score)
    optimize = commands.add_parser("optimize", help="hill-climb with GEPA")
    _common(optimize)
    optimize.add_argument("--proposer", choices=("codex", "none"), default="codex")
    optimize.add_argument("--max-metric-calls", type=int, default=30)
    optimize.add_argument("--max-proposals", type=int, default=8)
    optimize.add_argument("--seed", type=int, default=0)
    optimize.add_argument("--output", default="eval/runs/latest")
    optimize.set_defaults(handler=_optimize)
    smoke = commands.add_parser("smoke", help="run model-free cases through GEPA")
    _common(smoke)
    smoke.set_defaults(handler=_smoke)
    arguments = parser.parse_args(argv)
    return int(arguments.handler(arguments))


if __name__ == "__main__":
    raise SystemExit(main())
