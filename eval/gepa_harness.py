"""Thin integration with GEPA's universal optimize-anything API."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .scoring import EvaluationHarness

OBJECTIVE = """\
Improve the general-purpose Oxide planning prompt. Maximize exact source fidelity,
complete requirement disposition, honest readiness, dependency correctness, meaningful
verification planning, and robustness to formatting changes, missing acceptance criteria,
and contradictory specifications. Do not optimize for a fixed roadmap, stage count, product,
or fixture vocabulary. Preserve the Jinja variable contract and maintenance-mode behavior.
"""


def optimize_prompt(
    seed_template: str,
    evaluator: EvaluationHarness,
    *,
    proposer: Callable[..., dict[str, str]] | None,
    run_directory: Path,
    max_metric_calls: int = 30,
    max_candidate_proposals: int = 8,
    seed: int = 0,
) -> Any:
    """Run GEPA while keeping all planning evaluations sequential and bounded."""
    from gepa.optimize_anything import (
        EngineConfig,
        GEPAConfig,
        ReflectionConfig,
        optimize_anything,
    )

    run_directory.mkdir(parents=True, exist_ok=True)
    config = GEPAConfig(
        engine=EngineConfig(
            run_dir=str(run_directory),
            seed=seed,
            display_progress_bar=False,
            parallel=False,
            max_workers=1,
            use_cloudpickle=False,
            cache_evaluation=True,
            max_metric_calls=max_metric_calls,
            max_candidate_proposals=max_candidate_proposals,
        ),
        reflection=ReflectionConfig(
            skip_perfect_score=True,
            perfect_score=1.0,
            reflection_minibatch_size=min(3, len(evaluator.dataset())),
            custom_candidate_proposer=proposer,
            reflection_lm=None if proposer is not None else "openai/gpt-5.1",
        ),
    )
    return optimize_anything(
        seed_candidate={"planning_prompt": seed_template},
        evaluator=evaluator,
        dataset=evaluator.dataset(),
        objective=OBJECTIVE,
        config=config,
    )


def write_result(result: Any, destination: Path) -> tuple[Path, Path]:
    """Write the best prompt separately from GEPA's lineage/score receipt."""
    destination.mkdir(parents=True, exist_ok=True)
    candidate = result.best_candidate
    if not isinstance(candidate, dict) or set(candidate) != {"planning_prompt"}:
        raise RuntimeError("GEPA returned an unexpected candidate shape")
    prompt_path = destination / "planning.optimized.md.j2"
    receipt_path = destination / "gepa-result.json"
    prompt_path.write_text(candidate["planning_prompt"].rstrip() + "\n", encoding="utf-8")
    receipt_path.write_text(
        json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return prompt_path, receipt_path
