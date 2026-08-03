from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src" / "swarm_harness"


def test_runtime_has_only_the_small_two_tool_implementation() -> None:
    modules = {path.name for path in SOURCE.glob("*.py")}
    assert modules == {
        "__init__.py",
        "cli.py",
        "journal.py",
        "journal_mcp.py",
        "worker.py",
        "yaml_payload.py",
    }
    assert sum(path.read_text(encoding="utf-8").count("\n") for path in SOURCE.glob("*.py")) < 2000


def test_sqlite_is_confined_to_swappable_prototype() -> None:
    importers: list[str] = []
    for path in SOURCE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(name.name == "sqlite3" for name in node.names):
                importers.append(path.name)
            if isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
                importers.append(path.name)
    assert importers == ["journal.py"]


def test_no_old_coordination_api_survives() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in SOURCE.glob("*.py"))
    for forbidden in (
        "claim_task(",
        "heartbeat(",
        "proposal",
        "quorum",
        "lease_token",
        "validation_vote",
        "SQLiteJournal",
    ):
        assert forbidden not in source
