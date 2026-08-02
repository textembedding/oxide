from __future__ import annotations

import argparse
import json
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
    assert capsys.readouterr().out == "native macOS log\n"
