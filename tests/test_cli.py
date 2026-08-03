from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from swarm_harness import cli
from swarm_harness.concurrency import ConcurrencyError

ROOT = Path(__file__).parents[1]


def test_supervisor_reclaims_missing_worker_before_relaunch(monkeypatch) -> None:
    class Client:
        def search(self, run_id, query):
            assert (run_id, query) == ("run", "queue:all")
            return [{"state": "working", "worker_id": "worker-0"}]

        def add(self, run_id, author, text):
            assert (run_id, author, text) == (
                "run",
                "launcher",
                "control: reclaim worker:worker-0",
            )
            return {"saved": True, "reclaimed": "A"}

    launched, logged = [], []
    monkeypatch.setattr(cli, "_live_slots", lambda _config: set())
    monkeypatch.setattr(cli, "_launch_terminal", launched.append)
    supervisor = cli._Supervisor(
        {"workers": 2, "workload": "stage0", "run_id": "run"}, Client(), logged.append
    )
    supervisor.tick()
    assert logged == ["reclaimed A from crashed worker-0", "launched worker-0", "launched worker-1"]
    assert len(launched) == 2


def test_stage0_contract_parses_without_phantom_checks() -> None:
    stage = cli.load_stage(Path(__file__).parents[1] / "stages" / "stage0.yaml")
    assert stage["stage"] == "0"
    assert len(stage["tasks"]) == 16
    assert len(stage["tasks"][0]["checks"]) == 1
    assert len(stage["tasks"][-1]["depends_on"]) == 15
    assert len(stage["stage_gate"]) == 76


def test_controller_checks_use_native_macos_capability_denial(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run(command, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli.subprocess, "run", run)
    assert cli._run_checks(tmp_path, ["cargo test -p verifier"], lambda _line: None) == (True, "")
    assert calls[0][:3] == ["/usr/bin/sandbox-exec", "-p", cli._CHECK_PROFILE]
    assert calls[0][-3:] == ["/bin/zsh", "-lc", "cargo test -p verifier"]
    assert "(deny network*)" in cli._CHECK_PROFILE
    assert "(deny signal)" in cli._CHECK_PROFILE


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


def test_observer_normalizes_quoted_yaml_strings_before_highlighting() -> None:
    document = (
        '  - kind: "work"\n'
        '    title: "Readable title"\n'
        '    claim: "claim: task:A"\n'
        '    boolean: "true"\n'
        '    empty: ""\n'
        "    body: |-\n"
        "      unchanged"
    )
    assert cli._display_yaml(document) == (
        "  - kind: work\n"
        "    title: Readable title\n"
        "    claim: |-\n"
        "      claim: task:A\n"
        "    boolean: |-\n"
        "      true\n"
        "    empty: |-\n"
        "      \n"
        "    body: |-\n"
        "      unchanged"
    )


def test_observer_highlights_yaml_structure_without_coloring_journal_prose() -> None:
    rendered = cli._code(
        "text: |-\n"
        "  blocked: task:S0-POLICY-SEARCH-VERIFIERS\n"
        "  reason: The assigned revision requires fetching origin.\n"
        "verified: false",
        "yaml",
        True,
    )
    assert "\x1b[94mtext\x1b[39;49;00m:" in rendered
    assert "\x1b[94mverified\x1b[39;49;00m:" in rendered
    assert "\x1b[31m" not in rendered
    assert "\x1b[33m" not in rendered
    assert "blocked: task:S0-POLICY-SEARCH-VERIFIERS" in rendered
    assert "reason: The assigned revision requires fetching origin." in rendered


def test_worker_observer_animates_only_new_log_events(monkeypatch, tmp_path: Path, capsys) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "worker-0.log"
    history = {"type": "item.completed", "item": {"type": "agent_message", "text": "history"}}
    live = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "live line one\nlive line two"},
    }
    path.write_text(json.dumps(history) + "\n", encoding="utf-8")
    config = {"workers": 1, "run_dir": str(tmp_path), "run_id": "stage0-test"}
    states = iter(("initial", "running", "complete"))

    def context(_config, _slot):
        value = next(states)
        if value == "running":
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(live) + "\n")
        return "running" if value == "initial" else value, ("test-model", "task-a")

    delays = []
    monkeypatch.setattr(cli, "_load_config", lambda _workload: config)
    monkeypatch.setattr(cli, "_observer_context", context)
    monkeypatch.setattr(cli.time, "sleep", delays.append)
    arguments = cli.argparse.Namespace(
        workload="stage0", slot="worker-0", color="never", raw=False, no_follow=False
    )
    assert cli.command_observe(arguments) == 0
    output = capsys.readouterr().out
    assert output.index("history") < output.index("live line one") < output.index("live line two")
    assert delays[0] == 0.2
    assert sum(delays[1:]) == pytest.approx(1.0)


def test_worker_observer_bounds_initial_follow_replay(monkeypatch, tmp_path: Path, capsys) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    path = logs / "worker-0.log"
    path.write_text(
        json.dumps("old history")
        + "\n"
        + json.dumps("x" * 70_000)
        + "\n"
        + json.dumps("recent")
        + "\n",
        encoding="utf-8",
    )
    config = {"workers": 1, "run_dir": str(tmp_path), "run_id": "run"}
    monkeypatch.setattr(cli, "_load_config", lambda _workload: config)
    monkeypatch.setattr(
        cli, "_observer_context", lambda _config, _slot: ("complete", ("model", "task"))
    )
    arguments = cli.argparse.Namespace(
        workload="stage0", slot="worker-0", color="never", raw=False, no_follow=False
    )
    assert cli.command_observe(arguments) == 0
    output = capsys.readouterr().out
    assert "recent" in output
    assert "old history" not in output
    assert "x" * 100 not in output


def test_worker_observer_pins_only_model_and_task_in_bottom_row(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "worker-0.log").write_text(json.dumps("history") + "\n", encoding="utf-8")
    config = {"workers": 1, "run_dir": str(tmp_path), "run_id": "run", "model": "gpt-test"}
    context = ("complete", ("gpt-test", "S0-STABLE-SEAMS"))
    monkeypatch.setattr(cli, "_load_config", lambda _workload: config)
    monkeypatch.setattr(cli, "_observer_context", lambda _config, _slot: context)
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda **_kwargs: os.terminal_size((60, 20))
    )
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    arguments = cli.argparse.Namespace(
        workload="stage0", slot="worker-0", color="never", raw=False, no_follow=False
    )
    assert cli.command_observe(arguments) == 0
    output = capsys.readouterr().out
    assert "\x1b[?25l\x1b[?4h" in output
    footer = output.split("\x1b[2K\x1b[2;37;49m", 1)[1].split("\x1b[0m", 1)[0]
    assert footer.startswith("gpt-test")
    assert footer.endswith("S0-STABLE-SEAMS")
    assert not any(label in output for label in ("model:", "task:", "role:"))
    assert output.endswith("\x1b[?4l\x1b[?25h")


def test_queue_renders_append_only_records_in_chronological_order() -> None:
    snapshot = {
        "run_id": "stage0-20260802-123456",
        "state": "running",
        "entries": [
            {
                "record_id": 140,
                "author": "worker-0",
                "accepted": True,
                "body": "claim: task:ACTIVE-LONG-TASK-NAME",
            },
            {
                "record_id": 141,
                "author": "worker-1",
                "accepted": False,
                "body": "claim: task:ACTIVE-LONG-TASK-NAME",
            },
        ],
    }
    rendered = cli._render_queue(snapshot, color=False, width=40)
    colored = cli._render_queue(snapshot, color=True, width=40)
    assert "SWARM JOURNAL" in rendered
    assert "ACTIVE-LONG-TASK-NAME" in rendered
    assert rendered.index("JOURNAL #140") < rendered.index("JOURNAL #141")
    assert "author: worker-0\nstatus: accepted" in rendered
    assert "author: worker-1\nstatus: rejected" in rendered
    assert "[" not in rendered
    assert "-" * 40 in rendered
    assert "|" not in rendered
    assert "\x1b[1;36mSWARM JOURNAL\x1b[0m" in colored
    assert "\x1b[31mstatus: rejected\x1b[0m" in colored


def test_queue_outputs_and_truncates_journal_body() -> None:
    snapshot = {
        "run_id": "stage0-test",
        "state": "running",
        "entries": [
            {
                "record_id": 19,
                "author": "worker-2",
                "accepted": True,
                "body": "\n".join(f"journal line {number}" for number in range(1, 13)),
            }
        ],
    }
    rendered = cli._render_queue(snapshot, color=False)
    assert "JOURNAL #19" in rendered
    assert "journal line 1\n" in rendered
    assert "journal line 9\njournal line 10...\n" in rendered
    assert "journal line 11" not in rendered
    snapshot["entries"][0]["body"] = "activation-bound " * 30 + "\nHIDDEN"
    rendered = cli._render_queue(snapshot, color=False, width=40)
    body = rendered.split("status: accepted\n", 1)[1].split("-" * 40, 1)[0].splitlines()
    assert len(body) == 10
    assert all(len(line) <= 40 for line in body)
    assert body[-1].endswith("...")
    assert "HIDDEN" not in rendered


def test_following_queue_appends_new_records_without_redrawing(monkeypatch, capsys) -> None:
    first = {
        "state": "running",
        "run_id": "stage0-test",
        "entries": [{"record_id": 1, "author": "worker-0", "accepted": True, "body": "one"}],
    }
    second = {
        "state": "complete",
        "run_id": "stage0-test",
        "entries": [
            *first["entries"],
            {"record_id": 2, "author": "launcher", "accepted": True, "body": "two"},
        ],
    }
    snapshots = iter((first, second))
    delays = []
    monkeypatch.setattr(cli, "_load_config", lambda _workload: {})
    monkeypatch.setattr(cli, "_queue_snapshot", lambda _config: next(snapshots))
    monkeypatch.setattr(cli.time, "sleep", delays.append)
    arguments = cli.argparse.Namespace(workload="stage0", color="never", no_follow=False)
    assert cli.command_observe_queue(arguments) == 0
    output = capsys.readouterr().out
    assert output.count("SWARM JOURNAL") == 1
    assert output.index("JOURNAL #1") < output.index("JOURNAL #2")
    assert "\x1b[2J\x1b[H" not in output
    assert delays[0] == 1
    assert sum(delays[1:]) == pytest.approx(1.0)


def test_observer_reads_never_replace_the_live_journal(monkeypatch, tmp_path: Path) -> None:
    socket = tmp_path / "journal.sock"
    socket.touch()
    config = {"socket": str(socket), "database": str(tmp_path / "journal.db"), "run_id": "run"}
    monkeypatch.setattr(
        cli, "serve_in_thread", lambda *_args: pytest.fail("observer replaced journal server")
    )
    with pytest.raises(OSError, match="slow response"):
        cli._using_journal(config, lambda _client: (_ for _ in ()).throw(OSError("slow response")))

    class EmptyResponse:
        def __init__(self, _client) -> None:
            pass

        def _view(self, _run_id):
            raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(cli, "WorkflowClient", EmptyResponse)
    assert cli._observer_context(config, "worker-0") == (
        "starting",
        ("gpt 5.6 sol medium", "-"),
    )


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
