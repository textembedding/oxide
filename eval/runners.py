"""Model adapters for planning candidates, optional judging, and GEPA reflection."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from oxide.planning import PLAN_RESPONSE_SCHEMA, CodexSessionAgent, PlanningInfrastructureError

from .cases import EvaluationCase, Scenario


def _retry_infrastructure(operation: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Retry one transient model turn once; never translate it into a score."""
    for attempt in range(2):
        try:
            return operation()
        except PlanningInfrastructureError:
            if attempt == 1:
                raise
    raise AssertionError("unreachable")


class PlannerRunner(Protocol):
    def run(self, prompt: str, scenario: Scenario) -> dict[str, Any]: ...


class QualityJudge(Protocol):
    def score(
        self,
        case: EvaluationCase,
        base_response: dict[str, Any],
        variant_response: dict[str, Any],
    ) -> tuple[float, dict[str, Any]]: ...


class FixturePlannerRunner:
    """Model-free runner used to validate evaluation plumbing, not prompt quality."""

    def run(self, prompt: str, scenario: Scenario) -> dict[str, Any]:
        if not prompt.strip():
            raise RuntimeError("rendered planning prompt is empty")
        return {
            "message": "Fixture planning response.",
            "ready_for_approval": scenario.expected_approval,
            "complete_specification_corpus": True,
            "faithful_to_specifications": True,
            "unresolved": list(scenario.fixture_unresolved),
            "roadmap_markdown": scenario.model_free_output.read_text(encoding="utf-8"),
        }


@dataclass
class CodexPlannerRunner:
    repository: Path
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "max"
    timeout_seconds: float = 900.0
    absolute_timeout_seconds: float | None = None

    def run(self, prompt: str, scenario: Scenario) -> dict[str, Any]:
        del scenario
        return _retry_infrastructure(
            lambda: CodexSessionAgent(
                self.repository,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                timeout_seconds=self.timeout_seconds,
                absolute_timeout_seconds=self.absolute_timeout_seconds,
            ).start(prompt, PLAN_RESPONSE_SCHEMA)
        )


_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["faithfulness", "coverage", "decomposition", "readability", "reason"],
    "properties": {
        "faithfulness": {"type": "integer", "minimum": 0, "maximum": 4},
        "coverage": {"type": "integer", "minimum": 0, "maximum": 4},
        "decomposition": {"type": "integer", "minimum": 0, "maximum": 4},
        "readability": {"type": "integer", "minimum": 0, "maximum": 4},
        "reason": {"type": "string"},
    },
}


def _judge_source_bundle(case: EvaluationCase) -> str:
    """Include the base corpus once, followed by only a variant's real delta files."""
    blocks: list[str] = []
    seen: set[Path] = set()
    for label, scenario in (("BASE", case.base), ("VARIANT", case.variant)):
        for path in sorted((scenario.directory / "specs").rglob("*.md")):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            relative = path.relative_to(scenario.directory).as_posix()
            blocks.append(
                f"===== {label} SOURCE {relative} =====\n"
                + path.read_text(encoding="utf-8").rstrip()
            )
    return "\n\n".join(blocks)


@dataclass
class CodexQualityJudge:
    repository: Path
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "high"
    timeout_seconds: float = 600.0
    absolute_timeout_seconds: float | None = None

    def score(
        self,
        case: EvaluationCase,
        base_response: dict[str, Any],
        variant_response: dict[str, Any],
    ) -> tuple[float, dict[str, Any]]:
        prompt = f"""\
You are judging two planning outputs for an Oxide prompt evaluation. Mechanical checks run
separately and may have rejected either output. Do not assume its schema, citations, or dependency
graph are valid. Judge the complete supplied source corpus for the qualities that are not reliably
reducible to string checks, and use the reason field to identify systemic quality defects even
when an output is mechanically invalid:

1. faithfulness: goals and exclusions do not smuggle in product behavior absent from sources;
2. coverage: every material requirement, deferral, non-goal, and research question has one
   visible disposition rather than disappearing behind the representative oracles;
3. decomposition: boundaries are cohesive, useful, and no more serial than real dependencies;
4. readability: a human can understand outcomes, deferrals, and sequencing quickly.

Use 0 (unacceptable) through 4 (excellent). Do not reward matching a particular number of
stages. Do not waive a semantic defect because the prose sounds polished.

Case: {case.identifier} — {case.title}
Expected relation: {case.relation}

SOURCE CORPUS
{_judge_source_bundle(case)}

BASE RESPONSE
{json.dumps(base_response, ensure_ascii=False, sort_keys=True)}

VARIANT RESPONSE
{json.dumps(variant_response, ensure_ascii=False, sort_keys=True)}
"""
        result = _retry_infrastructure(
            lambda: CodexSessionAgent(
                self.repository,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                timeout_seconds=self.timeout_seconds,
                absolute_timeout_seconds=self.absolute_timeout_seconds,
            ).start(prompt, _JUDGE_SCHEMA)
        )
        values = [
            int(result[key]) for key in ("faithfulness", "coverage", "decomposition", "readability")
        ]
        return sum(values) / 16.0, result


_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["message", "planning_prompt"],
    "properties": {
        "message": {"type": "string"},
        "planning_prompt": {"type": "string"},
    },
}


@dataclass
class CodexPromptProposer:
    """Use the signed-in Codex CLI as GEPA's reflective mutation model."""

    repository: Path
    objective: str
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "max"
    timeout_seconds: float = 900.0
    absolute_timeout_seconds: float | None = None

    def __call__(
        self,
        candidate: dict[str, str],
        reflective_dataset: Mapping[str, Sequence[Mapping[str, Any]]],
        components_to_update: list[str],
    ) -> dict[str, str]:
        if components_to_update != ["planning_prompt"]:
            raise RuntimeError(f"unexpected GEPA components: {components_to_update}")
        prompt = f"""\
Improve Oxide's Jinja planning prompt using GEPA's evaluation feedback.

OBJECTIVE
{self.objective}

NON-NEGOTIABLE DROP-IN CONTRACT
- Return the complete replacement template, not a patch or commentary.
- Preserve every existing Jinja variable and maintenance-mode branch exactly as an input.
- Do not mention evaluation case names, fixture products, desired stage counts, or hidden labels.
- Generalize from failed invariants. Do not encode answers for the examples.
- Keep source semantics authoritative and Oxide's verification policy separate from product behavior.

CURRENT TEMPLATE
```jinja2
{candidate["planning_prompt"]}
```

GEPA ACTIONABLE FEEDBACK
{json.dumps(reflective_dataset, ensure_ascii=False, sort_keys=True, default=str)}
"""
        result = _retry_infrastructure(
            lambda: CodexSessionAgent(
                self.repository,
                model=self.model,
                reasoning_effort=self.reasoning_effort,
                timeout_seconds=self.timeout_seconds,
                absolute_timeout_seconds=self.absolute_timeout_seconds,
            ).start(prompt, _PROPOSAL_SCHEMA)
        )
        return {"planning_prompt": str(result["planning_prompt"])}
