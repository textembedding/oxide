from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

from swarm_harness import cli
from swarm_harness.sqlite_service import SQLiteJournal


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def create_run_config(runs: Path, target: Path, *, workload: str = "stage0") -> dict:
    run = runs / workload
    config = {
        "schema_version": 1,
        "run_id": f"{workload}-test",
        "workload": workload,
        "run_dir": str(run),
        "database": str(run / "journal.sqlite3"),
        "socket": str(run / "journal.sock"),
        "target_repo": str(target),
        "workers": 7,
    }
    run.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps(config), encoding="utf-8")
    return config


def seed_run(config: dict) -> SQLiteJournal:
    journal = SQLiteJournal(config["database"])
    journal.op_create_run(
        {
            "run_id": config["run_id"],
            "workload": config["workload"],
            "target_repo": config["target_repo"],
            "integration_branch": f"codex/swarm-{config['run_id']}/integration",
            "integration_worktree": str(Path(config["run_dir"]) / "integration"),
            "tasks": [
                {
                    "id": "S0-01",
                    "title": "First milestone",
                    "prompt": "Implement it",
                    "checks": [],
                    "depends_on": [],
                }
            ],
        }
    )
    return journal


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


def test_pause_and_resume_commands_preserve_run_configuration(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    runs = tmp_path / "runs"
    monkeypatch.setattr(cli, "RUNS", runs)
    config = create_run_config(runs, tmp_path / "target")
    journal = seed_run(config)
    journal.op_prepare_task(
        {
            "run_id": config["run_id"],
            "task_id": "S0-01",
            "branch": "codex/swarm-stage0-test/s0-01",
            "worktree_path": str(Path(config["run_dir"]) / "worktrees" / "s0-01"),
        }
    )
    journal.op_claim_task(
        {"run_id": config["run_id"], "worker_id": "worker-0", "lease_seconds": 60}
    )
    monkeypatch.setattr(cli, "_stop_run_processes", lambda _: None)

    assert cli.command_pause(argparse.Namespace(workload="stage0")) == 0
    paused = journal.op_run_status({"run_id": config["run_id"]})
    assert paused["run"]["state"] == "paused"
    assert paused["tasks"][0]["state"] == "pending"
    assert paused["tasks"][0]["worktree_path"].endswith("/worktrees/s0-01")

    launched: list[tuple[dict, bool]] = []
    monkeypatch.setattr(cli, "_run_processes", lambda _: [])
    monkeypatch.setattr(
        cli,
        "_start_run",
        lambda value, *, foreground: launched.append((value, foreground)) or 0,
    )
    assert cli.command_resume(argparse.Namespace(workload="stage0", foreground=False)) == 0
    assert journal.op_run_status({"run_id": config["run_id"]})["run"]["state"] == "running"
    assert launched == [(config, False)]
    assert launched[0][0]["workers"] == 7
    assert "1 active claim(s) fenced" in capsys.readouterr().out


def test_reset_removes_only_run_worktrees_and_branches_and_archives_state(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    git(target, "init", "-b", "main")
    git(target, "config", "user.name", "Harness Test")
    git(target, "config", "user.email", "harness@example.invalid")
    (target / "README.md").write_text("target\n", encoding="utf-8")
    git(target, "add", "README.md")
    git(target, "commit", "-m", "seed")

    runs = tmp_path / "state" / "runs"
    monkeypatch.setattr(cli, "RUNS", runs)
    config = create_run_config(runs, target)
    seed_run(config)
    run = Path(config["run_dir"])
    integration = run / "integration"
    task = run / "worktrees" / "s0-01"
    git(target, "worktree", "add", "-b", "codex/swarm-stage0-test/integration", str(integration))
    git(target, "worktree", "add", "-b", "codex/swarm-stage0-test/s0-01", str(task))
    git(target, "branch", "unrelated-branch")
    monkeypatch.setattr(cli, "_stop_run_processes", lambda _: None)

    assert cli.command_reset(argparse.Namespace(workload="stage0")) == 0

    archive = runs.parent / "archive" / "stage0-stage0-test"
    assert not run.exists()
    assert (archive / "run.json").is_file()
    assert "codex/swarm-stage0-test/" not in git(target, "branch", "--list")
    assert "unrelated-branch" in git(target, "branch", "--list")
    assert str(run) not in git(target, "worktree", "list", "--porcelain")
    output = capsys.readouterr().out
    assert f"archived prior state at {archive}" in output
    assert "Removed 2 worktree(s) and 2 run branch(es)." in output


def test_lifecycle_parser_selects_native_handlers() -> None:
    cases = {
        "pause": cli.command_pause,
        "resume": cli.command_resume,
        "reset": cli.command_reset,
    }
    for command, handler in cases.items():
        arguments = cli.build_parser().parse_args(
            ["harness", command, "--workload", "stage0"]
        )
        assert arguments.handler is handler
