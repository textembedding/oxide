from __future__ import annotations

import ast
from pathlib import Path

from swarm_harness.controller import load_stage

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "swarm_harness"
TESTS = ROOT / "tests"

ALLOWED_MODULES = {
    "__init__.py",
    "protocol.py",
    "sqlite_service.py",
    "journal_client.py",
    "live_observer.py",
    "tools.py",
    "controller.py",
    "worker.py",
    "cli.py",
}

FORBIDDEN_TERMS = {
    "bubblewrap",
    "cgroup",
    "mount namespace",
    "runtime closure",
    "artifact retirement",
    "process authority",
    "verifier broker",
    "recursive replay",
}


def _logical_lines(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                total += 1
    return total


def test_only_frozen_source_modules_exist() -> None:
    assert {path.name for path in SRC.glob("*.py")} == ALLOWED_MODULES


def test_source_line_budget() -> None:
    assert _logical_lines(sorted(SRC.glob("*.py"))) <= 5_000


def test_test_line_budget() -> None:
    assert _logical_lines(sorted(TESTS.glob("test_*.py"))) <= 3_000


def test_per_file_source_budget() -> None:
    for path in SRC.glob("*.py"):
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 2_000, path


def test_source_is_valid_python() -> None:
    for path in SRC.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_exactly_two_worker_tool_names_are_reserved() -> None:
    contract = (ROOT / "HARNESS_CONTRACT.md").read_text(encoding="utf-8")
    assert "claim_task" in contract
    assert "submit_result" in contract


def test_non_goals_remain_explicit() -> None:
    text = (ROOT / "NON_GOALS.md").read_text(encoding="utf-8").lower()
    for term in FORBIDDEN_TERMS:
        assert term in text


def test_stage_zero_is_enabled_for_pilot() -> None:
    path = ROOT / "stages" / "stage0.yaml"
    text = path.read_text(encoding="utf-8")
    stage = load_stage(path)
    expected = [
        "S0-STABLE-SEAMS",
        "S0-PROSPECTIVE-PROFILE",
        "S0-STORAGE-FEASIBILITY",
        "S0-EXECUTOR-FEASIBILITY",
        "S0-MODEL-FEASIBILITY",
        "S0-STATE-FEASIBILITY",
        "S0-RENDER-FEASIBILITY",
        "S0-REFERENCE-PROFILE",
        "S0-RESEARCH-BASIS",
        "S0-API-LEDGER-VERIFIERS",
        "S0-BLOCK-LANE-VERIFIERS",
        "S0-POLICY-SEARCH-VERIFIERS",
        "S0-PRESENTATION-VERIFIERS",
        "S0-COMPOSITION-VERIFIERS",
        "S0-RESEARCH-CORE-VERIFIERS",
        "S0-RESEARCH-DECISION-VERIFIERS",
    ]
    identifiers = [task["id"] for task in stage["tasks"]]
    assert stage["enabled"] is True
    assert identifiers == expected
    assert "S0-SEAL" not in text
    assert "verify_stage0_demo" not in text
    assert "completion.json" not in text

    seen: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    for task in stage["tasks"]:
        dependencies[task["id"]] = task["depends_on"]
        assert set(task["depends_on"]) <= seen
        seen.add(task["id"])

    closure: set[str] = set()
    pending = list(dependencies["S0-RESEARCH-DECISION-VERIFIERS"])
    while pending:
        dependency = pending.pop()
        if dependency not in closure:
            closure.add(dependency)
            pending.extend(dependencies[dependency])
    assert closure == set(expected[:-1])
    assert [task["id"] for task in stage["tasks"] if not task["depends_on"]] == expected[:-1]

    task_checks = [check for task in stage["tasks"] for check in task["checks"]]
    assert task_checks == stage["stage_gate"]
    commands = stage["stage_gate"][:-2]
    assert len(commands) == 74
    assert len(set(commands)) == 74
    assert sum("stage0_readiness" in command for command in commands) == 11
    assert sum("verify-feasibility" in command for command in commands) == 5
    assert sum("reference_profile_cycle" in command for command in commands) == 1
    assert sum(command.startswith("python3 -m research.verify ") for command in commands) == 10


def test_later_stages_remain_disabled() -> None:
    for number in range(1, 4):
        text = (ROOT / "stages" / f"stage{number}.yaml").read_text(encoding="utf-8")
        assert "enabled: false" in text
        assert "tasks: []" in text


def test_toy_stage_is_enabled() -> None:
    text = (ROOT / "stages" / "toy.yaml").read_text(encoding="utf-8")
    assert "enabled: true" in text
    assert all(task_id in text for task_id in ("TOY-01", "TOY-02", "TOY-03"))
