"""Thin integration with GEPA's universal optimize-anything API."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .identity import qualify_run_directory
from .scoring import EvaluationHarness

OBJECTIVE = """\
Improve Oxide's general-purpose Jinja planning prompt for unseen specification corpora.

Treat these priorities as lexicographic: a lower-priority gain must never weaken a
higher-priority invariant, and success on one corpus must not compensate for failure on another.

1. Preserve the drop-in contract: the exact Jinja variable set and each variable's meaning and
   control-flow role, schema-valid response behavior, frozen-bundle-only authority, qualified
   source records, mandated Oxide verification-policy behavior, and maintenance-mode preservation
   semantics.
2. Preserve source semantics completely: give every material requirement exactly one visible,
   source-backed disposition; invent no product behavior; use exact sufficient citations; and
   derive cohesive capability ownership, direct witnessed dependencies, phase-local readiness,
   honest top-level approval and unresolved state, localized gaps and contradictions, and
   role-appropriate verification from source semantics.
3. Make the semantic projection invariant and repeatable: semantically equivalent corpora retain
   capability boundaries, IDs, ownership, dependency graph, readiness, approval disposition,
   invariant placement, and material field content. A localized semantic change affects only its
   owning contract and unavoidable incident relations. Apply deterministic tie-breakers only after
   semantic correctness is fixed.
4. Make the rendered roadmap clear and reviewable. Compactness, concision, prompt or roadmap
   length, list length, citation count, and phase count are not quality objectives. Never omit,
   merge, split, or rewrite source-authorized work to make an output smaller; remove only
   unsupported filler, true duplicate ownership, and verbatim duplicate phase-owned narrative.

Generalize every mutation as a source-semantic rule for unseen products, corpus sizes, layouts,
and wording. Replace obsolete or conflicting rules instead of layering case exceptions. Never
encode or target case names, products, fixture paths or vocabulary, rubric identifiers, hidden
labels, expected IDs or stage counts, metric names or weights, diagnostic wording, judge phrasing,
or known answer patterns. Do not game coverage or consistency with broad citations, keyword
signals, boilerplate verification, constant decompositions, or evaluator blind spots. Keep
specification semantics authoritative and Oxide's verification policy separate from product
behavior.
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

    manifest = evaluator.bind_optimizer(proposer)
    qualify_run_directory(run_directory, manifest)
    dataset = evaluator.dataset()
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
            reflection_minibatch_size=min(3, len(dataset)),
            custom_candidate_proposer=proposer,
            reflection_lm=None if proposer is not None else "openai/gpt-5.1",
        ),
    )
    return optimize_anything(
        seed_candidate={"planning_prompt": seed_template},
        evaluator=evaluator,
        dataset=dataset,
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
