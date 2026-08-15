"""Model adapters for planning candidates, optional judging, and GEPA reflection."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from oxide.planning import PLAN_RESPONSE_SCHEMA, CodexSessionAgent

from .cases import EvaluationCase, Scenario


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

    def run(self, prompt: str, scenario: Scenario) -> dict[str, Any]:
        del scenario
        return CodexSessionAgent(
            self.repository,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            timeout_seconds=self.timeout_seconds,
        ).start(prompt, PLAN_RESPONSE_SCHEMA)


_JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["faithfulness", "decomposition", "readability", "reason"],
    "properties": {
        "faithfulness": {"type": "integer", "minimum": 0, "maximum": 4},
        "decomposition": {"type": "integer", "minimum": 0, "maximum": 4},
        "readability": {"type": "integer", "minimum": 0, "maximum": 4},
        "reason": {"type": "string"},
    },
}


@dataclass
class CodexQualityJudge:
    repository: Path
    model: str = "gpt-5.6-sol"
    reasoning_effort: str = "high"
    timeout_seconds: float = 600.0

    def score(
        self,
        case: EvaluationCase,
        base_response: dict[str, Any],
        variant_response: dict[str, Any],
    ) -> tuple[float, dict[str, Any]]:
        prompt = f"""\
You are judging two planning outputs for an Oxide prompt evaluation.
Deterministic checks have already validated schema, exact source citations, dependency
acyclicity, and declared requirement coverage. Judge only the qualities that are not
reliably reducible to string checks:

1. faithfulness: goals and exclusions do not smuggle in product behavior absent from sources;
2. decomposition: boundaries are cohesive, useful, and no more serial than real dependencies;
3. readability: a human can understand outcomes, deferrals, and sequencing quickly.

Use 0 (unacceptable) through 4 (excellent). Do not reward matching a particular number of
stages. Do not waive a semantic defect because the prose sounds polished.

Case: {case.identifier} — {case.title}
Expected relation: {case.relation}

BASE RESPONSE
{json.dumps(base_response, ensure_ascii=False, sort_keys=True)}

VARIANT RESPONSE
{json.dumps(variant_response, ensure_ascii=False, sort_keys=True)}
"""
        result = CodexSessionAgent(
            self.repository,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            timeout_seconds=self.timeout_seconds,
        ).start(prompt, _JUDGE_SCHEMA)
        values = [int(result[key]) for key in ("faithfulness", "decomposition", "readability")]
        return sum(values) / 12.0, result


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
        result = CodexSessionAgent(
            self.repository,
            model=self.model,
            reasoning_effort=self.reasoning_effort,
            timeout_seconds=self.timeout_seconds,
        ).start(prompt, _PROPOSAL_SCHEMA)
        return {"planning_prompt": str(result["planning_prompt"])}
