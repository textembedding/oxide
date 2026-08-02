from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from swarm_harness import cli


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
