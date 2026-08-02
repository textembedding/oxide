from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "swarm_harness"
TESTS = ROOT / "tests"

ALLOWED_MODULES = {
    "__init__.py",
    "protocol.py",
    "sqlite_service.py",
    "journal_client.py",
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
    assert _logical_lines(sorted(SRC.glob("*.py"))) <= 3_000


def test_test_line_budget() -> None:
    assert _logical_lines(sorted(TESTS.glob("test_*.py"))) <= 3_000


def test_per_file_source_budget() -> None:
    for path in SRC.glob("*.py"):
        assert len(path.read_text(encoding="utf-8").splitlines()) <= 500, path


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
    text = (ROOT / "stages" / "stage0.yaml").read_text(encoding="utf-8")
    assert "enabled: true" in text
    assert "S0-SEAL" in text


def test_later_stages_remain_disabled() -> None:
    for number in range(1, 4):
        text = (ROOT / "stages" / f"stage{number}.yaml").read_text(encoding="utf-8")
        assert "enabled: false" in text
        assert "tasks: []" in text


def test_toy_stage_is_enabled() -> None:
    text = (ROOT / "stages" / "toy.yaml").read_text(encoding="utf-8")
    assert "enabled: true" in text
    assert all(task_id in text for task_id in ("TOY-01", "TOY-02", "TOY-03"))
