from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path

from swarm_harness import cli
from swarm_harness.sqlite_service import SQLiteJournal


def test_native_observe_prints_slot_log(tmp_path: Path, monkeypatch, capsys) -> None:
    runs = tmp_path / "runs"
    monkeypatch.setattr(cli, "RUNS", runs)
    run = runs / "pilot"
    (run / "logs").mkdir(parents=True)
    (run / "logs" / "orchestrator.log").write_text("native macOS log\n", encoding="utf-8")
    (run / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": "pilot-test",
                "workload": "pilot",
                "run_dir": str(run),
                "database": str(run / "journal.sqlite3"),
                "socket": str(run / "journal.sock"),
                "workers": 1,
            }
        ),
        encoding="utf-8",
    )
    result = cli.command_observe(
        argparse.Namespace(workload="pilot", slot="orchestrator", no_follow=True)
    )
    assert result == 0
    transcript = capsys.readouterr().out
    assert "observer slot=orchestrator index=0" in transcript
    assert "invocation=pilot-test:orchestrator slot=orchestrator" in transcript
    assert transcript.endswith("native macOS log\n")


def test_observer_renders_codex_events_with_terminal_syntax() -> None:
    line = (
        '[12:34:56] {"type":"item.completed","item":'
        '{"type":"command_execution","command":"git diff",'
        '"aggregated_output":"+added\\n","exit_code":0,"status":"completed"}}'
    )
    plain = cli.highlight_stream_line(line, color=False)
    colored = cli.highlight_stream_line(line, color=True)
    assert "COMMAND COMPLETED status=completed exit=0" in plain
    assert "command:\n" in plain
    assert "+added" in plain
    assert "\x1b[" in colored


def test_completed_observer_exits_after_replaying_log(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    runs = tmp_path / "runs"
    monkeypatch.setattr(cli, "RUNS", runs)
    run = runs / "pilot"
    (run / "logs").mkdir(parents=True)
    (run / "logs" / "orchestrator.log").write_text("complete log\n", encoding="utf-8")
    database = run / "journal.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE runs(run_id TEXT, state TEXT)")
        connection.execute("INSERT INTO runs VALUES ('pilot-test', 'complete')")
    (run / "run.json").write_text(
        json.dumps(
            {
                "run_id": "pilot-test",
                "run_dir": str(run),
                "database": str(database),
                "socket": str(run / "missing.sock"),
                "workers": 1,
            }
        ),
        encoding="utf-8",
    )
    result = cli.command_observe(
        argparse.Namespace(workload="pilot", slot="orchestrator", no_follow=False)
    )
    assert result == 0
    transcript = capsys.readouterr().out
    assert "[observer] state=COMPLETE" in transcript
    assert transcript.endswith("complete log\n")


def test_auto_color_is_on_for_a_tty_even_with_no_color(monkeypatch) -> None:
    class Terminal:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(cli.sys, "stdout", Terminal())
    monkeypatch.setenv("NO_COLOR", "1")
    assert cli._observer_color("auto") is True
    assert cli._observer_color("never") is False


def test_queue_renderer_is_one_column_and_at_most_40_columns() -> None:
    now = time.time()
    snapshot = {
        "run_id": "pilot-20260802-very-long-run-identifier",
        "state": "running",
        "tasks": [
            {
                "task_id": "S0-01-VERY-LONG-MILESTONE-NAME",
                "state": "claimed",
                "claim_state": "active",
                "worker_id": "worker-0",
                "expires_at": now + 60,
                "blocked_count": 0,
                "accepted_commit": None,
                "submission_json": None,
                "last_error": None,
            },
            {
                "task_id": "S0-02",
                "state": "pending",
                "claim_state": None,
                "worker_id": None,
                "expires_at": None,
                "blocked_count": 1,
                "accepted_commit": None,
                "submission_json": None,
                "last_error": None,
            },
        ],
    }

    rendered = cli._render_queue(snapshot, color=True, width=40)
    plain = re.sub(r"\x1b\[[0-9;]*m", "", rendered)

    assert "WORKING\n" in plain
    assert "owner: worker-0\n" in plain
    assert "BLOCKED\n" in plain
    assert "waiting on: 1\n" in plain
    assert "|" not in plain
    assert max(len(line) for line in plain.splitlines()) <= 40


def test_observe_queue_reads_working_and_blocked_tasks(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    runs = tmp_path / "runs"
    monkeypatch.setattr(cli, "RUNS", runs)
    run = runs / "pilot"
    database = run / "journal.sqlite3"
    journal = SQLiteJournal(database)
    journal.op_create_run(
        {
            "run_id": "pilot-test",
            "workload": "pilot",
            "target_repo": str(tmp_path / "target"),
            "integration_branch": "codex/stage0",
            "integration_worktree": str(tmp_path / "integration"),
            "tasks": [
                {
                    "id": "S0-01",
                    "title": "First milestone",
                    "prompt": "Implement it",
                    "checks": [],
                    "depends_on": [],
                },
                {
                    "id": "S0-02",
                    "title": "Second milestone",
                    "prompt": "Implement it next",
                    "checks": [],
                    "depends_on": ["S0-01"],
                },
            ],
        }
    )
    journal.op_prepare_task(
        {
            "run_id": "pilot-test",
            "task_id": "S0-01",
            "branch": "codex/s0-01",
            "worktree_path": str(tmp_path / "s0-01"),
        }
    )
    journal.op_claim_task(
        {"run_id": "pilot-test", "worker_id": "worker-0", "lease_seconds": 60}
    )
    (run / "run.json").write_text(
        json.dumps(
            {
                "run_id": "pilot-test",
                "run_dir": str(run),
                "database": str(database),
                "workers": 1,
            }
        ),
        encoding="utf-8",
    )

    result = cli.command_observe_queue(
        argparse.Namespace(workload="pilot", no_follow=True, color="never")
    )
    transcript = capsys.readouterr().out

    assert result == 0
    assert "WORKING\nS0-01\nowner: worker-0\n" in transcript
    assert "BLOCKED\nS0-02\nwaiting on: 1\n" in transcript
    assert max(len(line) for line in transcript.splitlines()) <= 40


def test_observe_queue_parser_selects_queue_handler() -> None:
    arguments = cli.build_parser().parse_args(
        ["harness", "observe-queue", "--workload", "pilot", "--no-follow"]
    )
    assert arguments.handler is cli.command_observe_queue
