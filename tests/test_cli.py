from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from swarm_harness import cli
from swarm_harness.concurrency import ConcurrencyError

ROOT = Path(__file__).parents[1]


def test_stage0_contract_parses_without_phantom_checks() -> None:
    stage = cli.load_stage(Path(__file__).parents[1] / "stages" / "stage0.yaml")
    assert stage["stage"] == "0"
    assert len(stage["tasks"]) == 16
    assert len(stage["tasks"][0]["checks"]) == 1
    assert len(stage["tasks"][-1]["depends_on"]) == 15
    assert len(stage["stage_gate"]) == 76


def test_observer_ports_jsonl_highlighting_and_safe_indentation() -> None:
    event = {
        "type": "item.completed",
        "item": {
            "type": "mcp_tool_call",
            "server": "journal",
            "tool": "journal_add",
            "arguments": {"yaml": "text: |-\n  checkpoint: task:A\n  file\tname"},
            "result": {"content": [{"type": "text", "text": "saved: true\njournal_id: 7\n"}]},
        },
    }
    line = "[12:34:56] " + json.dumps(event)
    plain = cli.highlight_stream_line(line, color=False)
    colored = cli.highlight_stream_line(line, color=True)
    assert "TOOL COMPLETED journal.journal_add" in plain
    assert "input.yaml:\n  text:" in plain
    assert "file\\tname" in plain
    assert "output.yaml:" in plain
    assert "[12:34:56]" not in plain
    assert cli.highlight_stream_line(line, color=False, raw=True) == line
    assert "\x1b[" in colored
    assert json.dumps(event) not in colored


def test_observer_accepts_json_string_records() -> None:
    message = "ERROR apply_patch verification failed\nTraceback (most recent call last)"
    assert cli.highlight_stream_line(json.dumps(message), color=False) == message


def test_queue_is_single_column_bounded_and_shows_only_active_journal_progress() -> None:
    snapshot = {
        "run_id": "stage0-20260802-123456",
        "state": "running",
        "tasks": [
            {
                "task_id": "ACTIVE-LONG-TASK-NAME",
                "root_task_id": "ACTIVE-LONG-TASK-NAME",
                "state": "working",
                "role": "revision",
                "worker_id": "worker-0",
                "workflow_state": "authoring",
                "claim_state": "accepted",
                "generation": 2,
                "checkpoint": False,
                "handoff": False,
                "last_journal_record_id": 140,
                "last_journal_body": "claim: task:ACTIVE-LONG-TASK-NAME",
            },
            {"task_id": "READY", "state": "ready", "worker_id": None},
            {"task_id": "NOISE", "state": "blocked", "worker_id": None},
            {"task_id": "DONE", "state": "complete", "worker_id": None},
        ],
    }
    rendered = cli._render_queue(snapshot, color=False, width=40)
    colored = cli._render_queue(snapshot, color=True, width=40)
    assert "ACTIVE-LONG-TASK-NAME" in rendered
    assert "READY" not in rendered
    assert "NOISE" not in rendered
    assert "DONE" not in rendered
    assert "worker-0 is revising candidate 3" in rendered
    assert "step: editing files" in rendered
    assert "journal #140" in rendered
    assert "body:\nclaim: task:ACTIVE-LONG-TASK-NAME" in rendered
    assert "detail:" not in rendered
    assert "authoring" not in rendered
    assert "claim accepted" not in rendered
    assert "checkpoint no" not in rendered
    assert "-" * 40 in rendered
    assert "|" not in rendered
    assert "\x1b[1;33mIN PROGRESS\x1b[0m" in colored
    assert "\x1b[1;34mworker-0 is revising" in colored


def test_queue_outputs_journal_body() -> None:
    snapshot = {
        "run_id": "stage0-test",
        "state": "running",
        "tasks": [
            {
                "task_id": "A",
                "state": "working",
                "role": "revision",
                "worker_id": "worker-2",
                "generation": 1,
                "checkpoint": True,
                "handoff": False,
                "last_journal_record_id": 19,
                "last_journal_body": (
                    "checkpoint: task:A\nfiles: artifacts/*\n"
                    "status: executable compatibility probes now pass"
                ),
            }
        ],
    }
    rendered = cli._render_queue(snapshot, color=False)
    assert "journal #19" in rendered
    assert (
        "body:\ncheckpoint: task:A\nfiles: artifacts/*\n"
        "status: executable compatibility probes now pass"
    ) in rendered


def test_following_queue_redraws_one_terminal_view(monkeypatch, capsys) -> None:
    snapshot = {"state": "complete", "tasks": [], "run_id": "stage0-test"}
    monkeypatch.setattr(cli, "_load_config", lambda _workload: {})
    monkeypatch.setattr(cli, "_queue_snapshot", lambda _config: snapshot)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    arguments = cli.argparse.Namespace(workload="stage0", color="never", no_follow=False)
    assert cli.command_observe_queue(arguments) == 0
    assert capsys.readouterr().out.startswith("\x1b[2J\x1b[H")


def test_macos_commands_and_controls_remain_available() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["verify"]).handler is cli.command_verify
    run = parser.parse_args(["harness", "run", "--workload", "stage0", "--target", "/tmp/memory"])
    assert run.workers == 7
    assert run.reviews == 3
    configured = parser.parse_args(
        [
            "harness",
            "run",
            "--workload",
            "stage0",
            "--target",
            "/tmp/memory",
            "--reviews",
            "2",
        ]
    )
    assert configured.reviews == 2
    concurrency = parser.parse_args(["harness", "validate-concurrency"])
    assert concurrency.handler is cli.command_validate_concurrency
    assert concurrency.workers == 7
    assert concurrency.rounds == 6
    for command in ("pause", "resume", "reset", "observe", "observe-queue", "status"):
        arguments = ["harness", command, "--workload", "stage0"]
        if command == "observe":
            arguments += ["--slot", "worker-0"]
        parsed = parser.parse_args(arguments)
        assert parsed.workload == "stage0"


def test_stage0_is_blocked_before_staging_without_a_qualified_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    arguments = cli.build_parser().parse_args(
        [
            "harness",
            "run",
            "--workload",
            "stage0",
            "--target",
            str(tmp_path / "target"),
            "--workers",
            "7",
        ]
    )
    monkeypatch.setattr(cli, "load_stage", lambda _: {"stage": "0"})
    monkeypatch.setattr(cli, "_config_path", lambda _: tmp_path / "run.json")

    def reject(_root: Path, _receipt: Path, *, required_workers: int):
        assert required_workers == 7
        raise ConcurrencyError("qualification required")

    monkeypatch.setattr(cli, "validate_receipt", reject)
    with pytest.raises(ConcurrencyError, match="qualification required"):
        cli.command_run(arguments)
    assert not (tmp_path / "run.json").exists()


def test_native_launcher_worker_mcp_and_git_complete_toy_stage(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "test@example.com"], check=True
    )
    (target / "README.md").write_text("fixture\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(target), "commit", "-qm", "seed"], check=True)

    run_dir = ROOT / ".swarm" / "runs" / "smoke"
    assert not run_dir.exists()
    environment = os.environ.copy()
    environment["PATH"] = str(ROOT / "tests" / "fake-bin") + os.pathsep + environment["PATH"]
    environment["SWARM_NO_TERMINAL"] = "1"
    result = subprocess.run(
        [
            str(ROOT / "swarmctl"),
            "harness",
            "run",
            "--workload",
            "smoke",
            "--target",
            str(target),
            "--workers",
            "4",
            "--foreground",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    config = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert config["schema_version"] == 4
    assert config["required_reviews"] == 3
    assert "integration_branch" not in config
    assert config["branch_prefix"].startswith("codex/swarm-smoke-")
    assert (
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "show",
                f"{config['target_branch']}:toy-output/combined.txt",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        == "one\ntwo\n"
    )
    assert (target / "toy-output" / "combined.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    target_tip = subprocess.run(
        ["git", "-C", str(target), "rev-parse", config["target_branch"]],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    assert target_tip != config["base_commit"]
    merge_count = int(
        subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "rev-list",
                "--count",
                "--merges",
                f"{config['base_commit']}..{target_tip}",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
    )
    assert merge_count == 3
    task_refs = subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "for-each-ref",
            "--format=%(refname:short)",
            f"refs/heads/{config['branch_prefix']}/",
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.splitlines()
    assert len(task_refs) == 3
    assert "verified reviewed main " in (run_dir / "logs" / "orchestrator.log").read_text(
        encoding="utf-8"
    )
    worker_logs = "\n".join(
        path.read_text(encoding="utf-8") for path in (run_dir / "logs").glob("worker-*.log")
    )
    assert '"tool": "journal_search"' in worker_logs
    assert '"tool": "journal_add"' in worker_logs
    assert "approve: review:" in worker_logs
    assert "merge: task:" in worker_logs

    reset = subprocess.run(
        [str(ROOT / "swarmctl"), "harness", "reset", "--workload", "smoke"],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert reset.returncode == 0, reset.stdout + reset.stderr
    assert not run_dir.exists()
    assert not subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "for-each-ref",
            "--format=%(refname)",
            f"refs/heads/{config['branch_prefix']}/",
        ],
        text=True,
        capture_output=True,
        check=True,
    ).stdout
