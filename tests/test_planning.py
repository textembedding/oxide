from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from oxide import cli
from oxide.alignment import AlignmentError, validate_alignment_receipt
from oxide.contract import load_contract
from oxide.planning import (
    CodexSessionAgent,
    PlanningError,
    ScriptedAgent,
    ScriptedUser,
    _contract_prompt,
    _plan_prompt,
    run_generate_contract_session,
    run_plan_session,
    select_contract_phases,
)
from oxide.prompt_templates import PromptTemplateError, render_prompt
from oxide.roadmap import (
    ROADMAP_VIEW_MARKER,
    RoadmapError,
    canonical_source_text,
    load_roadmap,
    parse_roadmap,
    render_roadmap_document,
    stage_binding,
)
from oxide.verification_policy import POLICY_PROFILE, verification_policy_digest

SPEC = """\
# Journal product

## Global invariants

Every acknowledged append remains durable after restart.

## Durable journal

Implement an append-only journal with durable recovery.

## Exact retrieval

Add bounded exact search.

## Semantic retrieval

Add threshold-qualified semantic candidates.

## Maintenance

Add checkpointing and compaction.
"""


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "product"
    (repository / "docs" / "specs").mkdir(parents=True)
    (repository / "verification").mkdir()
    (repository / "docs" / "specs" / "PRODUCT.md").write_text(SPEC, encoding="utf-8")
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "add specifications")
    return repository


def _fake_codex(tmp_path: Path, program: str) -> Path:
    executable = tmp_path / "codex"
    executable.write_text(f"#!{sys.executable}\n{program}", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_codex_session_streams_progress_and_closes_child_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_codex(
        tmp_path,
        """\
import json
import pathlib
import sys

assert sys.stdin.read() == "Plan this repository."
arguments = sys.argv[1:]
assert 'model_reasoning_effort="max"' in arguments
assert 'model_reasoning_summary="detailed"' in arguments
assert arguments[arguments.index("--model") + 1] == "gpt-5.6-sol"
assert arguments.count("-") == 1
output = pathlib.Path(arguments[arguments.index("--output-last-message") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "planning-thread-123"}), flush=True)
print(json.dumps({"type": "turn.started"}), flush=True)
print(json.dumps({"type": "item.started", "item": {"type": "command_execution", "command": "rg --files docs/specs"}}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "Checking specification coverage"}}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ready"}}), flush=True)
print(json.dumps({"type": "turn.completed"}), flush=True)
output.write_text(json.dumps({"message": "ready"}), encoding="utf-8")
""",
    )
    monkeypatch.setattr("oxide.planning.shutil.which", lambda _name: str(executable))
    progress: list[str] = []
    agent = CodexSessionAgent(tmp_path, timeout_seconds=2, progress=progress.append)

    assert agent.start("Plan this repository.", {"type": "object"}) == {"message": "ready"}
    assert agent.identity == "codex/planning-thread-123"
    assert any("reading the specifications" in line for line in progress)
    assert any("repository inspection" in line for line in progress)
    assert any("Codex reasoning: Checking specification coverage" in line for line in progress)
    assert any("Codex progress: ready" in line for line in progress)
    assert any("reasoning: max" in line for line in progress)


def test_codex_session_rejects_unknown_reasoning_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_codex(tmp_path, "")
    monkeypatch.setattr("oxide.planning.shutil.which", lambda _name: str(executable))

    with pytest.raises(PlanningError, match="reasoning effort must be one of"):
        CodexSessionAgent(tmp_path, reasoning_effort="extreme")


def test_codex_progress_extracts_message_from_structured_response() -> None:
    event = {
        "type": "item.completed",
        "item": {
            "type": "agent_message",
            "text": json.dumps(
                {
                    "message": "Roadmap ready.",
                    "roadmap_markdown": "# very long proposal",
                }
            ),
        },
    }

    assert CodexSessionAgent._event_progress(event) == "Codex progress: Roadmap ready."


def test_codex_progress_exposes_reasoning_summary_items() -> None:
    event = {
        "type": "item.completed",
        "item": {
            "type": "reasoning",
            "summary": [
                {"type": "summary_text", "text": "Map source requirements"},
                {"type": "summary_text", "text": "Check roadmap coverage"},
            ],
        },
    }

    assert CodexSessionAgent._event_progress(event) == (
        "Codex reasoning: Map source requirements / Check roadmap coverage"
    )


def test_codex_session_times_out_and_stops_silent_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_codex(
        tmp_path,
        """\
import json
import time

print(json.dumps({"type": "thread.started", "thread_id": "silent-thread"}), flush=True)
time.sleep(60)
""",
    )
    monkeypatch.setattr("oxide.planning.shutil.which", lambda _name: str(executable))
    progress: list[str] = []
    agent = CodexSessionAgent(
        tmp_path,
        timeout_seconds=0.15,
        heartbeat_seconds=0.03,
        progress=progress.append,
    )

    with pytest.raises(PlanningError, match="no events for 0.15 seconds"):
        agent.start("Plan this repository.", {"type": "object"})
    assert any("still running" in line for line in progress)


def test_codex_session_timeout_tracks_inactivity_not_total_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = _fake_codex(
        tmp_path,
        """\
import json
import pathlib
import sys
import time

arguments = sys.argv[1:]
output = pathlib.Path(arguments[arguments.index("--output-last-message") + 1])
print(json.dumps({"type": "thread.started", "thread_id": "active-thread"}), flush=True)
for index in range(5):
    time.sleep(0.1)
    print(json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": f"step {index}"}}), flush=True)
output.write_text(json.dumps({"message": "ready"}), encoding="utf-8")
""",
    )
    monkeypatch.setattr("oxide.planning.shutil.which", lambda _name: str(executable))
    progress: list[str] = []
    agent = CodexSessionAgent(
        tmp_path,
        timeout_seconds=0.25,
        heartbeat_seconds=0.05,
        progress=progress.append,
    )

    assert agent.start("Plan this repository.", {"type": "object"}) == {"message": "ready"}
    assert any("Codex reasoning: step 4" in line for line in progress)


def test_plan_prompt_preloads_complete_frozen_corpus_without_repository_rereads(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    second = repository / "docs" / "specs" / "RESEARCH.md"
    second.write_text("# Research\n\nMeasure recovery latency.\n", encoding="utf-8")
    (tmp_path / "ROADMAP.md").write_text("WITHHELD EVALUATION LABEL\n", encoding="utf-8")

    prompt = _plan_prompt(repository, "docs/specs")

    assert SPEC in prompt
    assert "Measure recovery latency." in prompt
    assert '"path":"docs/specs/PRODUCT.md"' in prompt
    assert '"path":"docs/specs/RESEARCH.md"' in prompt
    assert "WITHHELD EVALUATION LABEL" not in prompt
    assert "Do not use shell, file, Git, or network tools to reread it" in prompt
    assert "Stage 0 through Stage 3" not in prompt
    assert "never assume a particular stage count" in prompt
    assert "complete source-defined horizon" in prompt
    assert "planned, deferred, or blocked stage" in prompt
    assert "perform a second coverage pass over every source heading" in prompt
    assert "standardized, human-readable projection" in prompt
    assert "BEGIN OXIDE NORMATIVE VERIFICATION POLICY" in prompt
    assert POLICY_PROFILE in prompt
    assert verification_policy_digest() in prompt
    assert "All production logic is verified by default." in prompt
    assert "The Rust verification landscape" not in prompt
    assert "docs/specs/VERIFICATION.md" not in prompt


def test_planning_prompt_template_injects_maintenance_values(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    prompt = _plan_prompt(
        repository,
        "docs/specs",
        maintenance_stage_ids=["stage-1"],
        maintenance_request="Promote semantic search after its dependency is ready.",
    )

    assert "MAINTENANCE MODE" in prompt
    assert '["stage-1"]' in prompt
    assert "Promote semantic search after its dependency is ready." in prompt


def test_packaged_prompt_templates_fail_closed_on_missing_values() -> None:
    rendered = render_prompt(
        "planning-follow-up",
        kind="schema-repair",
        problem="missing stage outcome",
    )

    assert "failed mechanical schema validation" in rendered
    assert "missing stage outcome" in rendered
    with pytest.raises(PromptTemplateError, match="cannot render prompt template planning"):
        render_prompt("planning")
    with pytest.raises(PromptTemplateError, match="unknown prompt template"):
        render_prompt("not-a-prompt")


def _roadmap(
    *,
    status: str = "ready",
    stage0_requirement: str = "Implement an append-only journal with durable recovery.",
    stage0_verification: str = "Prove append durability and recovery refinement.",
) -> str:
    stages = [
        (
            "stage-0",
            "Deliver the durable journal foundation.",
            "Durable journal",
            stage0_requirement,
            [],
            "ready",
            stage0_verification,
        ),
        (
            "stage-1",
            "Deliver bounded exact search.",
            "Exact retrieval",
            "Add bounded exact search.",
            ["stage-0"],
            "planned",
            "Prove exact result preservation.",
        ),
        (
            "stage-2",
            "Deliver semantic candidate admission.",
            "Semantic retrieval",
            "Add threshold-qualified semantic candidates.",
            ["stage-1"],
            "planned",
            "Prove semantic candidates cannot gain authority.",
        ),
        (
            "stage-3",
            "Deliver checkpointing and compaction.",
            "Maintenance",
            "Add checkpointing and compaction.",
            ["stage-2"],
            "planned",
            "Prove observational preservation.",
        ),
    ]
    stage_blocks = []
    for identifier, outcome, anchor, requirement, dependencies, readiness, verification in stages:
        stage_blocks.append(
            f'''\
[[stages]]
id = "{identifier}"
outcome = "{outcome}"
included_scope = ["{requirement}"]
excluded_scope = ["Capabilities assigned to later phases"]
dependencies = {json.dumps(dependencies)}
source_specifications = [{{ path = "docs/specs/PRODUCT.md", anchor = "{anchor}", requirement = "{requirement}" }}]
applicable_global_invariants = ["durability"]
implementation_goals = ["{requirement}"]
verification_goals = ["{verification}"]
readiness = "{readiness}"
'''
        )
    return f'''\
# Journal implementation roadmap

<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Journal implementation roadmap"
status = "{status}"
specification_root = "docs/specs"

[[global_invariants]]
id = "durability"
statement = "Every acknowledged append remains durable after restart."
sources = [{{ path = "docs/specs/PRODUCT.md", anchor = "Global invariants", requirement = "Every acknowledged append remains durable after restart." }}]

{"".join(stage_blocks)}```
'''


def _plan_response(roadmap: str, *, ready: bool = True, message: str = "Roadmap ready.") -> dict:
    return {
        "message": message,
        "ready_for_approval": ready,
        "complete_specification_corpus": ready,
        "faithful_to_specifications": ready,
        "unresolved": [] if ready else ["Capability priority awaits user direction"],
        "roadmap_markdown": roadmap,
    }


def test_roadmap_document_has_one_schema_and_a_deterministic_human_view() -> None:
    raw = "Agent-authored prose that must not become a second plan.\n\n" + _roadmap()

    rendered = render_roadmap_document(raw)

    assert rendered.startswith(ROADMAP_VIEW_MARKER + "\n# Roadmap")
    assert "Agent-authored prose" not in rendered
    assert "| Phase | Outcome | Readiness | Depends on |" in rendered
    assert "[`stage-0`](#phase-stage-0)" in rendered
    assert "### 1. `stage-0` — Deliver the durable journal foundation." in rendered
    assert "#### Scope" in rendered
    assert "#### Verification goals" in rendered
    assert "[PRODUCT.md — Durable journal](docs/specs/PRODUCT.md#durable-journal)" in rendered
    assert "<summary>Show authoritative TOML</summary>" in rendered
    assert "<!-- oxide-roadmap-schema:1 -->" in rendered
    assert parse_roadmap(rendered) == parse_roadmap(raw)
    assert render_roadmap_document(rendered) == rendered


def test_roadmap_accepts_policy_owned_assurance_invariants_without_target_citations() -> None:
    roadmap = (
        _roadmap()
        .replace(
            '[[global_invariants]]\nid = "durability"',
            '[[global_invariants]]\nid = "pervasive-verus"\nstatement = "Every production logical component is verified."\nsources = []\n\n[[global_invariants]]\nid = "durability"',
        )
        .replace(
            'applicable_global_invariants = ["durability"]',
            'applicable_global_invariants = ["durability", "pervasive-verus"]',
        )
    )

    value = parse_roadmap(roadmap)

    policy_invariant = next(
        item for item in value["global_invariants"] if item["id"] == "pervasive-verus"
    )
    assert policy_invariant["sources"] == []
    assert "**Authority:** Governing verification policy" in render_roadmap_document(roadmap)


def _contract(
    *,
    requirement: str = "Implement an append-only journal with durable recovery.",
    verification_goal: str = "Prove append durability and recovery refinement.",
) -> str:
    return f'''\
schema = 5
id = "journal-phases"
stages = ["stage-0"]
enabled = true
goal = "Deliver the durable journal foundation."
minimum_reviews = 3
verification_policy_sha256 = "{verification_policy_digest()}"
immutable_paths = [
  "ROADMAP.md",
  "docs/specs",
  "verification/contract.toml",
  "verification/manifest.toml",
  "verification/toolchain.lock.toml",
]

[alignment]
specifications = ["docs/specs/PRODUCT.md"]
roadmap = "ROADMAP.md"
roadmap_stages = ["stage-0"]
contractible = true
implementation_goals = ["{requirement}"]
verification_goals = ["{verification_goal}"]
goal_sources = [{{ specification = "docs/specs/PRODUCT.md", anchor = "Durable journal", requirement = "{requirement}" }}]
ambiguities = []
missing_acceptance_criteria = []
unsupported_assumptions = []
semantic_gaps = []
proposed_revisions = []

[execution]
evidence_policy = "exact-verus-context-v1"

[[tasks]]
id = "append-recovery"
phase = "stage-0"
title = "Implement durable append and recovery"
prompt = "Implement the approved durable append and recovery behavior with its Verus proof."
depends_on = []
sources = [{{ specification = "docs/specs/PRODUCT.md", anchor = "Durable journal", requirement = "{requirement}" }}]

[[tasks.checks]]
id = "append-recovery-proof"
driver = "command"
command = "cargo test --all-targets"
sources = [{{ specification = "docs/specs/PRODUCT.md", anchor = "Durable journal", requirement = "{requirement}" }}]
'''


def _contract_response(
    roadmap: str,
    contract: str,
    *,
    ready: bool = True,
    verification_goal: str | list[str] = "Prove append durability and recovery refinement.",
    file_updates: list[dict] | None = None,
) -> dict:
    gaps = {
        "ambiguities": [],
        "missing_acceptance_criteria": [],
        "unsupported_assumptions": [],
        "semantic_gaps": [],
    }
    if not ready:
        gaps["ambiguities"] = ["Recovery success is not precise enough"]
    verification_goals = (
        [verification_goal] if isinstance(verification_goal, str) else verification_goal
    )
    return {
        "message": "Contract and exact verification goals are ready."
        if ready
        else "Clarify recovery.",
        "ready_for_approval": ready,
        "contractible": ready,
        "faithful_to_sources": ready,
        "complete_specification_corpus": True,
        "unresolved": gaps,
        "verification_goals": verification_goals,
        "file_updates": file_updates or [],
        "roadmap_markdown": roadmap,
        "contract_toml": contract,
    }


def _approved_plan(repository: Path) -> None:
    run_plan_session(
        repository / "docs" / "specs",
        agent=ScriptedAgent([_plan_response(_roadmap())]),
        user=ScriptedUser(["/approve"]),
        user_identity={"name": "Test User", "email": "test@example.com"},
    )


def _two_phase_contract() -> str:
    durable_requirement = "Implement an append-only journal with durable recovery."
    exact_requirement = "Add bounded exact search."
    contract = _contract()
    contract = (
        contract.replace('stages = ["stage-0"]', 'stages = ["stage-0", "stage-1"]')
        .replace(
            'goal = "Deliver the durable journal foundation."',
            'goal = "Deliver the durable journal foundation and bounded exact search."',
        )
        .replace('roadmap_stages = ["stage-0"]', 'roadmap_stages = ["stage-0", "stage-1"]')
        .replace(
            f'implementation_goals = ["{durable_requirement}"]',
            f'implementation_goals = ["{durable_requirement}", "{exact_requirement}"]',
        )
        .replace(
            'verification_goals = ["Prove append durability and recovery refinement."]',
            'verification_goals = ["Prove append durability and recovery refinement.", '
            '"Prove exact result preservation."]',
        )
    )
    return (
        contract
        + f'''\

[[tasks]]
id = "bounded-exact-search"
phase = "stage-1"
title = "Implement bounded exact search"
prompt = "Implement the approved bounded exact search behavior and proof."
depends_on = ["append-recovery"]
sources = [{{ specification = "docs/specs/PRODUCT.md", anchor = "Exact retrieval", requirement = "{exact_requirement}" }}]

[[tasks.checks]]
id = "bounded-exact-search-proof"
driver = "command"
command = "cargo test --all-targets"
sources = [{{ specification = "docs/specs/PRODUCT.md", anchor = "Exact retrieval", requirement = "{exact_requirement}" }}]
'''
    )


def test_contract_prompt_injects_oxide_policy_outside_target_semantic_closure(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)

    prompt = _contract_prompt(repository, "ROADMAP.md", ["stage-0"])

    assert "BEGIN OXIDE NORMATIVE VERIFICATION POLICY" in prompt
    assert verification_policy_digest() in prompt
    assert "All production logic is verified by default." in prompt
    assert "docs/specs/VERIFICATION.md" not in prompt
    assert '"path": "docs/specs/PRODUCT.md"' in prompt


def test_source_trace_ignores_formatting_but_not_semantic_changes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    specification = repository / "docs" / "specs" / "PRODUCT.md"
    specification.write_text(
        SPEC.replace(
            "Implement an append-only journal with durable recovery.",
            "Implement an append-only journal\n    with durable recovery.",
        ),
        encoding="utf-8",
    )
    assert stage_binding(repository, "ROADMAP.md", "stage-0")["stage_id"] == "stage-0"

    specification.write_text(
        specification.read_text(encoding="utf-8").replace(
            "durable recovery", "best-effort recovery"
        ),
        encoding="utf-8",
    )
    with pytest.raises(RoadmapError, match="source requirement is absent"):
        stage_binding(repository, "ROADMAP.md", "stage-0")


def test_source_canonicalization_preserves_code_and_semantics() -> None:
    assert canonical_source_text("Alpha  beta\n gamma") == "Alpha beta gamma"
    assert canonical_source_text("Alpha `x  y` beta") == "Alpha x  y beta"
    assert canonical_source_text("Every command is **planned**.") == (
        canonical_source_text("Every command is planned.")
    )
    assert canonical_source_text("Run `cargo  test` now.") != canonical_source_text(
        "Run `cargo test` now."
    )
    assert canonical_source_text("Alpha beta") != canonical_source_text("alpha beta")


def _approved_contract(repository: Path) -> None:
    run_generate_contract_session(
        repository / "ROADMAP.md",
        "stage-0",
        agent=ScriptedAgent([_contract_response(_roadmap(), _contract())]),
        user=ScriptedUser(["/approve"]),
        user_identity={"name": "Test User", "email": "test@example.com"},
    )


def _commit_approvals(repository: Path) -> tuple[Path, dict]:
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "approve durable journal contract")
    contract = repository / "verification" / "contract.toml"
    stage = load_contract(contract)
    result = validate_alignment_receipt(
        repository, _git(repository, "rev-parse", "HEAD"), contract, stage
    )
    return contract, result


def test_plan_pushback_refines_agent_derived_boundaries_and_requires_approval(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    user = ScriptedUser(["Move durability ahead of search.", "/approve"])
    path = run_plan_session(
        repository / "docs" / "specs",
        agent=ScriptedAgent(
            [
                _plan_response(_roadmap(status="draft"), ready=False),
                _plan_response(_roadmap(), message="Durability remains the first phase."),
            ]
        ),
        user=user,
        user_identity={"name": "Test User", "email": "test@example.com"},
    )
    assert path == repository / "ROADMAP.md"
    written = path.read_text(encoding="utf-8")
    assert written.startswith(ROADMAP_VIEW_MARKER)
    assert "## At a glance" in written
    assert "## Implementation phases" in written
    assert all(f'id = "stage-{number}"' in written for number in range(4))
    roadmap = load_roadmap(path)
    assert [stage["readiness"] for stage in roadmap["stages"]] == [
        "ready",
        "planned",
        "planned",
        "planned",
    ]
    assert not any((repository / "verification").iterdir())
    assert "Move durability ahead of search." in user.transcript


def test_plan_maintenance_changes_only_the_selected_phase(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    promoted = _roadmap().replace('readiness = "planned"', 'readiness = "ready"', 1)
    user = ScriptedUser(["Promote stage-1 from planned to ready.", "/approve"])

    run_plan_session(
        repository / "docs" / "specs",
        agent=ScriptedAgent([_plan_response(promoted)]),
        user=user,
        user_identity={"name": "Test User", "email": "test@example.com"},
        update_stage_ids=["stage-1"],
    )

    roadmap = load_roadmap(repository / "ROADMAP.md")
    assert [stage["readiness"] for stage in roadmap["stages"]] == [
        "ready",
        "ready",
        "planned",
        "planned",
    ]
    assert any("stage-1: readiness" in item for item in user.transcript)
    assert any("no dependent phase approvals invalidated" in item for item in user.transcript)


def test_plan_maintenance_rejects_unselected_phase_changes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    selected = _roadmap().replace('readiness = "planned"', 'readiness = "ready"', 1)
    unscoped = selected.replace('readiness = "planned"', 'readiness = "ready"', 1)
    user = ScriptedUser(
        [
            "Promote only stage-1.",
            "/approve",
        ]
    )

    run_plan_session(
        repository / "docs" / "specs",
        agent=ScriptedAgent([_plan_response(unscoped), _plan_response(selected)]),
        user=user,
        user_identity={"name": "Test User", "email": "test@example.com"},
        update_stage_ids=["stage-1"],
    )

    roadmap = load_roadmap(repository / "ROADMAP.md")
    assert roadmap["stages"][2]["readiness"] == "planned"
    assert any("changed unselected phase 'stage-2'" in item for item in user.transcript)


def test_plan_maintenance_reports_dependent_phase_invalidation(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    revised = _roadmap().replace(
        "Deliver bounded exact search.",
        "Deliver bounded exact retrieval as the next capability.",
    )
    user = ScriptedUser(["Clarify the stage-1 outcome.", "/approve"])

    run_plan_session(
        repository / "docs" / "specs",
        agent=ScriptedAgent([_plan_response(revised)]),
        user=user,
        user_identity={"name": "Test User", "email": "test@example.com"},
        update_stage_ids=["stage-1"],
    )

    assert any(
        "dependent phase approvals invalidated: stage-2, stage-3" in item
        for item in user.transcript
    )


def test_plan_repairs_agent_schema_errors_before_requesting_user_review(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    invalid = _roadmap().replace(
        'id = "stage-0"\n',
        'id = "stage-0"\npriority = 1\n',
        1,
    )
    user = ScriptedUser(["/approve"])

    path = run_plan_session(
        repository / "docs" / "specs",
        agent=ScriptedAgent(
            [
                _plan_response(invalid),
                _plan_response(_roadmap()),
            ]
        ),
        user=user,
        user_identity={"name": "Test User", "email": "test@example.com"},
    )

    assert path == repository / "ROADMAP.md"
    assert "priority = 1" not in path.read_text(encoding="utf-8")
    assert any("asking it to repair" in item for item in user.transcript)
    assert user.transcript.count("Feedback, /approve this exact roadmap, or /quit: ") == 1


def test_plan_without_explicit_approval_writes_nothing(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(PlanningError, match="cancelled"):
        run_plan_session(
            repository / "docs" / "specs",
            agent=ScriptedAgent([_plan_response(_roadmap())]),
            user=ScriptedUser(["/quit"]),
            user_identity={"name": "Test User", "email": "test@example.com"},
        )
    assert not (repository / "ROADMAP.md").exists()
    assert not (repository / ".oxide").exists()


def test_roadmap_human_view_cannot_diverge_from_approved_machine_data(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    path = repository / "ROADMAP.md"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "Deliver the durable journal foundation.",
            "Misleading human-only outcome.",
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(RoadmapError, match="human view changes the meaning"):
        stage_binding(repository, "ROADMAP.md", "stage-0")


def test_roadmap_human_view_allows_presentation_only_changes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    path = repository / "ROADMAP.md"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace("**Readiness:** Ready", "Readiness: Ready", 1)
        .replace("#### Scope", "##### Scope", 1),
        encoding="utf-8",
    )

    assert stage_binding(repository, "ROADMAP.md", "stage-0")["stage_id"] == "stage-0"


def test_plan_keeps_invalid_source_trace_inside_the_collaborative_loop(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    invalid = _roadmap(stage0_requirement="Invented behavior absent from the specification.")
    user = ScriptedUser(["/approve"])
    run_plan_session(
        repository / "docs" / "specs",
        agent=ScriptedAgent(
            [
                _plan_response(invalid),
                _plan_response(_roadmap()),
            ]
        ),
        user=user,
        user_identity={"name": "Test User", "email": "test@example.com"},
    )
    assert (
        "Invented behavior absent from the specification."
        not in (repository / "ROADMAP.md").read_text()
    )
    assert any("source requirement is absent" in item for item in user.transcript)
    assert user.transcript.count("Feedback, /approve this exact roadmap, or /quit: ") == 1


def test_plan_can_admit_agent_proposed_single_stage_boundary(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    (repository / "docs" / "specs" / "PRODUCT.md").write_text(
        "# Product\n\nReturn the stored value.\n", encoding="utf-8"
    )
    one_stage = """\
# Small product roadmap

<!-- oxide-roadmap-schema:1 -->
```toml
schema = 1
title = "Small product roadmap"
status = "ready"
specification_root = "docs/specs"
global_invariants = []

[[stages]]
id = "stage-0"
outcome = "Deliver the complete small product."
included_scope = ["Return the stored value."]
excluded_scope = []
dependencies = []
source_specifications = [{ path = "docs/specs/PRODUCT.md", anchor = "Product", requirement = "Return the stored value." }]
applicable_global_invariants = []
implementation_goals = ["Return the stored value."]
verification_goals = ["Prove every input returns its stored value."]
readiness = "ready"
```
"""
    run_plan_session(
        repository / "docs" / "specs",
        agent=ScriptedAgent([_plan_response(one_stage)]),
        user=ScriptedUser(["/approve"]),
        user_identity={"name": "Test User", "email": "test@example.com"},
    )
    assert [item["id"] for item in load_roadmap(repository / "ROADMAP.md")["stages"]] == ["stage-0"]


def test_contract_generation_rejects_an_approved_but_unready_stage(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    promoted = _roadmap().replace('readiness = "planned"', 'readiness = "ready"', 1)
    (repository / "ROADMAP.md").write_text(render_roadmap_document(promoted), encoding="utf-8")
    with pytest.raises(PlanningError, match="not ready"):
        run_generate_contract_session(
            repository / "ROADMAP.md",
            ["stage-0", "stage-1", "stage-2"],
            agent=ScriptedAgent([]),
            user=ScriptedUser([]),
            user_identity={"name": "Test User", "email": "test@example.com"},
        )
    assert not (repository / "verification" / "contract.toml").exists()


def test_contract_refinement_backpropagates_and_regenerates_before_approval(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    refined_requirement = (
        "Implement an append-only journal whose acknowledged entries survive process restart."
    )
    refined_goal = "Prove acknowledged entries survive every modeled process restart."
    refined_spec = SPEC.replace(
        "Implement an append-only journal with durable recovery.", refined_requirement
    )
    first = _contract_response(_roadmap(), _contract(), ready=False)
    second_roadmap = _roadmap(
        stage0_requirement=refined_requirement, stage0_verification=refined_goal
    )
    second = _contract_response(
        second_roadmap,
        _contract(requirement=refined_requirement, verification_goal=refined_goal),
        verification_goal=refined_goal,
        file_updates=[
            {
                "path": "docs/specs/PRODUCT.md",
                "content": refined_spec,
                "reason": "Define recovery success in human-readable product semantics.",
            }
        ],
    )
    user = ScriptedUser(["Define recovery as survival across process restart.", "/approve"])
    run_generate_contract_session(
        repository / "ROADMAP.md",
        "stage-0",
        agent=ScriptedAgent([first, second]),
        user=user,
        user_identity={"name": "Test User", "email": "test@example.com"},
    )
    assert refined_requirement in (repository / "docs" / "specs" / "PRODUCT.md").read_text()
    refined_roadmap = (repository / "ROADMAP.md").read_text(encoding="utf-8")
    assert refined_roadmap.startswith(ROADMAP_VIEW_MARKER)
    assert refined_goal in refined_roadmap
    assert refined_goal in (repository / "verification" / "contract.toml").read_text()
    assert not (repository / ".oxide").exists()
    contract, result = _commit_approvals(repository)
    assert result["schema"] == "OxideEmbeddedAlignmentV1"
    assert load_contract(contract)["alignment"]["verification_goals"] == [refined_goal]


def test_contract_embeds_attestation_and_approval_without_sidecars(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    _approved_contract(repository)
    contract = repository / "verification" / "contract.toml"
    text = contract.read_text(encoding="utf-8")
    assert "[binding]" in text
    assert "[attestation]" in text
    assert "[approval]" in text
    assert {path.name for path in (repository / "verification").iterdir()} == {"contract.toml"}

    contract.write_text(text.replace("approved = true", "approved = false"), encoding="utf-8")
    with pytest.raises(Exception, match="approval"):
        load_contract(contract)


def test_relevant_change_invalidates_but_deferred_future_stage_edit_does_not(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    _approved_contract(repository)
    contract, _ = _commit_approvals(repository)
    stage = load_contract(contract)
    specification = repository / "docs" / "specs" / "PRODUCT.md"
    specification.write_text(SPEC.replace("Add bounded exact search.", "Add exact query search."))
    _git(repository, "add", str(specification))
    _git(repository, "commit", "-qm", "refine deferred exact retrieval")
    validate_alignment_receipt(repository, _git(repository, "rev-parse", "HEAD"), contract, stage)
    specification.write_text(
        specification.read_text().replace(
            "Implement an append-only journal with durable recovery.",
            "Implement an append-only journal with best-effort recovery.",
        )
    )
    _git(repository, "add", str(specification))
    _git(repository, "commit", "-qm", "alter selected durable journal capability")
    with pytest.raises(AlignmentError, match="changed|closure|approval|absent"):
        validate_alignment_receipt(
            repository, _git(repository, "rev-parse", "HEAD"), contract, stage
        )


def test_formatting_only_selected_source_edit_preserves_contract_approval(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    _approved_contract(repository)
    contract, _ = _commit_approvals(repository)
    stage = load_contract(contract)
    specification = repository / "docs" / "specs" / "PRODUCT.md"
    specification.write_text(
        specification.read_text().replace(
            "Implement an append-only journal with durable recovery.",
            "Implement an **append-only journal**\n  with durable recovery.",
        ),
        encoding="utf-8",
    )
    _git(repository, "add", str(specification))
    _git(repository, "commit", "-qm", "reformat selected requirement")

    result = validate_alignment_receipt(
        repository, _git(repository, "rev-parse", "HEAD"), contract, stage
    )
    assert result["stage_ids"] == ["stage-0"]


def test_selected_stage_or_global_invariant_change_invalidates_contract(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    _approved_contract(repository)
    contract, _ = _commit_approvals(repository)
    stage = load_contract(contract)
    roadmap = repository / "ROADMAP.md"
    roadmap.write_text(
        roadmap.read_text().replace(
            "Deliver the durable journal foundation.",
            "Deliver a changed durable journal foundation.",
        )
    )
    _git(repository, "add", str(roadmap))
    _git(repository, "commit", "-qm", "change selected stage")
    with pytest.raises(AlignmentError):
        validate_alignment_receipt(
            repository, _git(repository, "rev-parse", "HEAD"), contract, stage
        )


def test_applicable_global_invariant_change_invalidates_contract(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    _approved_contract(repository)
    contract, _ = _commit_approvals(repository)
    stage = load_contract(contract)
    specification = repository / "docs" / "specs" / "PRODUCT.md"
    specification.write_text(
        specification.read_text().replace(
            "Every acknowledged append remains durable after restart.",
            "Acknowledged appends may be lost after restart.",
        )
    )
    _git(repository, "add", str(specification))
    _git(repository, "commit", "-qm", "change global invariant")
    with pytest.raises(AlignmentError):
        validate_alignment_receipt(
            repository, _git(repository, "rev-parse", "HEAD"), contract, stage
        )


def test_contract_semantics_absent_from_roadmap_closure_fail_qualification(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    invented = _contract().replace(
        'requirement = "Implement an append-only journal with durable recovery."',
        'requirement = "Delete all entries after restart."',
    )
    response = _contract_response(_roadmap(), invented)
    with pytest.raises(PlanningError, match="cancelled"):
        run_generate_contract_session(
            repository / "ROADMAP.md",
            "stage-0",
            agent=ScriptedAgent([response, response]),
            user=ScriptedUser(["/approve", "/quit"]),
            user_identity={"name": "Test User", "email": "test@example.com"},
        )
    assert not (repository / "verification" / "contract.toml").exists()


def test_negative_flow_cannot_create_run_state_or_manual_alignment_receipt(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    with pytest.raises(PlanningError, match="cancelled"):
        run_generate_contract_session(
            repository / "ROADMAP.md",
            "stage-0",
            agent=ScriptedAgent([_contract_response(_roadmap(), _contract())]),
            user=ScriptedUser(["/quit"]),
            user_identity={"name": "Test User", "email": "test@example.com"},
        )
    assert not (repository / ".oxide").exists()
    assert not any((repository / "verification").iterdir())


def test_unapproved_or_ambiguous_planning_cannot_be_silently_admitted(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    with pytest.raises(PlanningError, match="ran out"):
        run_plan_session(
            repository / "docs" / "specs",
            agent=ScriptedAgent([_plan_response(_roadmap(status="draft"), ready=False)]),
            user=ScriptedUser(["/approve"]),
            user_identity={"name": "Test User", "email": "test@example.com"},
        )
    assert not (repository / "ROADMAP.md").exists()


def test_run_admission_fails_before_state_when_interactive_artifacts_are_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _repository(tmp_path)
    (repository / "ROADMAP.md").write_text(_roadmap(), encoding="utf-8")
    (repository / "verification" / "contract.toml").write_text(_contract(), encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-qm", "add unapproved generated artifacts")
    run_root = tmp_path / "oxide-runs"
    monkeypatch.setattr(cli, "RUNS", run_root)
    assert (
        cli.main(
            [
                "harness",
                "run",
                "--target",
                str(repository),
                "--workers",
                "1",
            ]
        )
        == 2
    )
    assert not run_root.exists()
    assert not (repository / ".oxide").exists()


def test_end_to_end_scripted_transcript_covers_both_approval_boundaries(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    planning_user = ScriptedUser(["Keep the proposed capability order.", "/approve"])
    run_plan_session(
        repository / "docs" / "specs",
        agent=ScriptedAgent(
            [
                _plan_response(_roadmap(status="draft"), ready=False),
                _plan_response(_roadmap()),
            ]
        ),
        user=planning_user,
        user_identity={"name": "Test User", "email": "test@example.com"},
    )
    contract_user = ScriptedUser(["Keep recovery proof explicit.", "/approve"])
    run_generate_contract_session(
        repository / "ROADMAP.md",
        "stage-0",
        agent=ScriptedAgent(
            [
                _contract_response(_roadmap(), _contract(), ready=False),
                _contract_response(_roadmap(), _contract()),
            ]
        ),
        user=contract_user,
        user_identity={"name": "Test User", "email": "test@example.com"},
    )
    contract, binding = _commit_approvals(repository)
    assert binding["schema"] == "OxideEmbeddedAlignmentV1"
    assert binding["agent_identity"] == "fake-agent/test"
    assert binding["approved_by"]["email"] == "test@example.com"
    assert load_contract(contract)["stage"] == "stage-0"
    assert "Keep the proposed capability order." in planning_user.transcript
    assert "Keep recovery proof explicit." in contract_user.transcript


def test_checked_phases_generate_one_aggregate_contract(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    _approved_plan(repository)
    selected_roadmap = _roadmap().replace('readiness = "planned"', 'readiness = "ready"', 1)
    (repository / "ROADMAP.md").write_text(
        render_roadmap_document(selected_roadmap), encoding="utf-8"
    )
    goals = [
        "Prove append durability and recovery refinement.",
        "Prove exact result preservation.",
    ]

    path = run_generate_contract_session(
        repository / "ROADMAP.md",
        ["stage-0", "stage-1"],
        agent=ScriptedAgent(
            [
                _contract_response(
                    selected_roadmap,
                    _two_phase_contract(),
                    verification_goal=goals,
                )
            ]
        ),
        user=ScriptedUser(["/approve"]),
        user_identity={"name": "Test User", "email": "test@example.com"},
    )

    contract = load_contract(path)
    assert contract["stages"] == ["stage-0", "stage-1"]
    assert [task["phase"] for task in contract["tasks"]] == ["stage-0", "stage-1"]
    assert contract["alignment"]["verification_goals"] == goals
    assert "[binding]" in path.read_text(encoding="utf-8")


def test_phase_selector_requires_ready_dependencies_and_supports_multiple_phases() -> None:
    roadmap = parse_roadmap(
        _roadmap()
        .replace('readiness = "planned"', 'readiness = "ready"', 1)
        .replace('readiness = "planned"', 'readiness = "ready"', 1)
    )
    user = ScriptedUser(["2", "1", "2", "3", "/confirm"])

    assert select_contract_phases(roadmap, user) == ["stage-0", "stage-1", "stage-2"]
    assert any("Select dependencies first: stage-0" in item for item in user.transcript)
    assert any("stage-3" in item and "not ready" in item for item in user.transcript)


def test_phase_selector_refuses_unchecking_a_selected_dependency() -> None:
    roadmap = parse_roadmap(_roadmap().replace('readiness = "planned"', 'readiness = "ready"', 1))
    user = ScriptedUser(["1", "2", "1", "/confirm"])

    assert select_contract_phases(roadmap, user) == ["stage-0", "stage-1"]
    assert any("Uncheck dependent phases first: stage-1" in item for item in user.transcript)
