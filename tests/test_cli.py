from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest

from swarm_harness import cli
from swarm_harness.concurrency import (
    ROLES,
    ConcurrencyError,
    implementation_digest,
    kernel_digest,
)
from swarm_harness.journal_backend import start_journal
from swarm_harness.workflow import WorkflowClient, WorkflowError

ROOT = Path(__file__).parents[1]


def _workload_text() -> str:
    return """\
stage: foundation
enabled: true
goal: Build and verify a small web application.
tasks:
  - id: API
    title: Build the API
    prompt: Implement the HTTP API.
    depends_on: []
    checks:
      - npm test -- api
  - id: UI
    title: Build the UI
    prompt: Implement the browser UI.
    depends_on: [API]
    checks:
      - npm test -- ui
stage_gate:
  - npm test
"""


def _write_receipt(path: Path, *, workers: int) -> Path:
    archived = path / "campaign" / "report.json"
    archived.parent.mkdir(parents=True)
    report = {
        "schema": "swarm-concurrency-validation-v1",
        "status": "passed",
        "source_digest": implementation_digest(ROOT),
        "kernel_digest": kernel_digest(None),
        "min_exact": 5,
        "max_results": 10,
        "workers": workers,
        "rounds": 4,
        "seed": 7,
        "roles": list(ROLES),
        "invariants": {
            "same_claim_observed": True,
            "one_effective_owner": True,
            "losers_did_no_protected_work": True,
            "winner_crash_recovered_by_replay": True,
            "all_worker_replays_identical": True,
            "complete_replay_beyond_query_limit": True,
        },
        "report_path": str(archived.resolve()),
    }
    report["receipt_digest"] = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    archived.write_text(json.dumps(report), encoding="utf-8")
    latest = path / "latest.json"
    latest.write_text(json.dumps(report), encoding="utf-8")
    return latest


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
        {"workers": 2, "workload": "web-app", "run_id": "run"}, Client(), logged.append
    )
    supervisor.tick()
    assert logged == ["reclaimed A from crashed worker-0", "launched worker-0", "launched worker-1"]
    assert len(launched) == 2


def test_arbitrary_product_contract_parses_without_rewriting_checks(tmp_path: Path) -> None:
    path = tmp_path / "web-app.yaml"
    path.write_text(_workload_text(), encoding="utf-8")
    workload = cli.load_stage(path)
    assert workload["stage"] == "foundation"
    assert [task["id"] for task in workload["tasks"]] == ["API", "UI"]
    assert workload["tasks"][1]["depends_on"] == ["API"]
    assert workload["tasks"][0]["checks"] == ["npm test -- api"]
    assert workload["stage_gate"] == ["npm test"]


def test_workload_contract_rejects_dependency_cycles(tmp_path: Path) -> None:
    path = tmp_path / "cycle.yaml"
    path.write_text(
        _workload_text().replace("depends_on: []", "depends_on: [UI]"), encoding="utf-8"
    )
    with pytest.raises(cli.HarnessError, match="cycle"):
        cli.load_stage(path)


def test_target_harness_directory_cannot_escape_through_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    outside = tmp_path / "outside"
    target.mkdir()
    outside.mkdir()
    (target / "swarm-harness").symlink_to(outside, target_is_directory=True)
    with pytest.raises(cli.HarnessError, match="symlink"):
        cli._stage_path(target, "web-app")


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
    assert "\x1b[1;34minput.yaml\x1b[0m:" in colored
    assert "\x1b[38;5;214m" in colored
    assert "\x1b[38;5;208m" not in colored
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


def test_observer_highlights_yaml_fields_and_colors_input_values_as_strings() -> None:
    rendered = cli._code(
        "text: |-\n"
        "  blocked: task:SEARCH-API\n"
        "  reason: The assigned revision requires fetching origin.\n"
        "verified: false",
        "yaml-input",
        True,
    )
    orange = "\x1b[38;5;214m"
    assert f"\x1b[94mtext\x1b[0m:{orange} |-\x1b[0m" in rendered
    assert f"\x1b[94mblocked\x1b[0m:{orange} task:SEARCH-API\x1b[0m" in rendered
    assert (
        f"\x1b[94mreason\x1b[0m:{orange} The assigned revision requires fetching origin.\x1b[0m"
        in rendered
    )
    assert f"\x1b[94mverified\x1b[0m:{orange} false\x1b[0m" in rendered
    assert "\x1b[31m" not in rendered
    assert "\x1b[33m" not in rendered
    source = cli._code('value = "quoted string"', "python", True)
    assert orange in source
    assert "\x1b[33m" not in source


def test_observer_uses_codex_base_colors_and_colored_section_labels() -> None:
    agent = cli._event_value(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "Use `semantic` and `map` as prose."},
        },
        True,
    )
    command = cli._event_value(
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "command": "printf result",
                "aggregated_output": "plain command output",
            },
        },
        True,
    )
    assert "\x1b[1;32mAGENT\x1b[0m [\x1b[2mCOMPLETED\x1b[0m]" in agent
    assert "\x1b[38;5;153mUse `semantic` and `map` as prose.\x1b[0m" in agent
    assert "\x1b[38;5;214m" not in agent
    assert "\x1b[1;34mcommand\x1b[0m:" in command
    assert "\x1b[1;34moutput\x1b[0m:" in command
    assert "\x1b[38;5;250mplain command output\x1b[0m" in command


def test_observer_highlights_displayed_source_without_guessing_for_other_commands() -> None:
    command = (
        "/bin/zsh -lc \"sed -n '1,260p' app/server.py && sed -n '1,180p' tests/test_server.py\""
    )
    assert cli._command_output_language(command) == "python"
    assert (
        cli._command_output_language("sed -n '1,80p' source.rs && rg schema manifest.json")
        == "rust"
    )
    assert cli._command_output_language("npm test -- contracts/search.json") == ""


def test_observer_prints_file_change_diff(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    source = tmp_path / "example.py"
    source.write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "example.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=tmp_path, check=True)
    source.write_text("value = 2\n", encoding="utf-8")
    event = {
        "type": "item.completed",
        "item": {
            "type": "file_change",
            "changes": [{"path": str(source), "kind": "update"}],
            "status": "completed",
        },
    }

    rendered = cli._event_value(event, False, tmp_path)

    assert "FILES COMPLETED" in rendered
    assert "changed:\n  - example.py" in rendered
    assert "diff --git a/example.py b/example.py" in rendered
    assert "-value = 1" in rendered
    assert "+value = 2" in rendered


def test_observer_never_guesses_a_lexer_for_raw_output(monkeypatch) -> None:
    def fail(_language: str):
        raise AssertionError("raw text must not request a lexer")

    monkeypatch.setattr(cli, "get_lexer_by_name", fail)
    rendered = cli._code(
        "Retrieval composition uses semantic map insertion as ordinary prose.",
        "",
        True,
        "38;5;250",
    )
    assert rendered == (
        "\x1b[38;5;250mRetrieval composition uses semantic map insertion as ordinary prose.\x1b[0m"
    )


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
    config = {"workers": 1, "run_dir": str(tmp_path), "run_id": "web-app-test"}
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
        workload="web-app", slot="worker-0", color="never", raw=False, no_follow=False
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
        workload="web-app", slot="worker-0", color="never", raw=False, no_follow=False
    )
    assert cli.command_observe(arguments) == 0
    output = capsys.readouterr().out
    assert "recent" in output
    assert "old history" not in output
    assert "x" * 100 not in output


def test_worker_observer_pins_only_model_and_role_in_bottom_row(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "worker-0.log").write_text(json.dumps("history") + "\n", encoding="utf-8")
    config = {"workers": 1, "run_dir": str(tmp_path), "run_id": "run", "model": "gpt-test"}
    context = ("complete", ("gpt-test", "implementation"))
    monkeypatch.setattr(cli, "_load_config", lambda _workload: config)
    monkeypatch.setattr(cli, "_observer_context", lambda _config, _slot: context)
    monkeypatch.setattr(
        cli.shutil, "get_terminal_size", lambda **_kwargs: os.terminal_size((60, 20))
    )
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    arguments = cli.argparse.Namespace(
        workload="web-app", slot="worker-0", color="never", raw=False, no_follow=False
    )
    assert cli.command_observe(arguments) == 0
    output = capsys.readouterr().out
    assert "\x1b[?25l\x1b[?4h" in output
    footer = output.split("\x1b[2K\x1b[2;37;49m", 1)[1].split("\x1b[0m", 1)[0]
    assert footer.startswith("gpt-test")
    assert footer.endswith("implementation")
    assert not any(label in output for label in ("model:", "task:", "role:"))
    assert output.endswith("\x1b[?4l\x1b[?25h")


def test_worker_observer_refreshes_role_while_log_is_streaming(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "worker-0.log").write_text(json.dumps("streaming") + "\n", encoding="utf-8")
    config = {"workers": 1, "run_dir": str(tmp_path), "run_id": "run"}
    contexts = iter(
        (
            ("running", ("gpt-test", "implementation")),
            ("running", ("gpt-test", "review")),
            ("complete", ("gpt-test", "verification")),
        )
    )
    ticks = iter((0.0, 1.0))
    monkeypatch.setattr(cli, "_load_config", lambda _workload: config)
    monkeypatch.setattr(cli, "_observer_context", lambda _config, _slot: next(contexts))
    monkeypatch.setattr(cli.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    arguments = cli.argparse.Namespace(
        workload="web-app", slot="worker-0", color="never", raw=False, no_follow=False
    )

    assert cli.command_observe(arguments) == 0
    output = capsys.readouterr().out
    assert "review" in output
    assert output.index("streaming") < output.index("review") < output.index("verification")


def test_queue_renders_append_only_records_in_chronological_order() -> None:
    snapshot = {
        "run_id": "web-app-20260802-123456",
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
        "run_id": "web-app-test",
        "state": "running",
        "entries": [
            {
                "record_id": 19,
                "author": "worker-2",
                "accepted": True,
                "body": "\n".join(f"journal line {number}" for number in range(1, 23)),
            }
        ],
    }
    rendered = cli._render_queue(snapshot, color=False)
    assert "JOURNAL #19" in rendered
    assert "journal line 1\n" in rendered
    assert "journal line 19\njournal line 20...\n" in rendered
    assert "journal line 21" not in rendered
    snapshot["entries"][0]["body"] = "activation-bound " * 60 + "\nHIDDEN"
    rendered = cli._render_queue(snapshot, color=False, width=40)
    body = rendered.split("status: accepted\n", 1)[1].split("-" * 40, 1)[0].splitlines()
    assert len(body) == 20
    assert all(len(line) <= 40 for line in body)
    assert body[-1].endswith("...")
    assert "HIDDEN" not in rendered


def test_following_queue_appends_new_records_without_redrawing(monkeypatch, capsys) -> None:
    first = {
        "state": "running",
        "run_id": "web-app-test",
        "entries": [{"record_id": 1, "author": "worker-0", "accepted": True, "body": "one"}],
    }
    second = {
        "state": "complete",
        "run_id": "web-app-test",
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
    arguments = cli.argparse.Namespace(workload="web-app", color="never", no_follow=False)
    assert cli.command_observe_queue(arguments) == 0
    output = capsys.readouterr().out
    assert output.count("SWARM JOURNAL") == 1
    assert output.index("JOURNAL #1") < output.index("JOURNAL #2")
    assert "\x1b[2J\x1b[H" not in output
    assert delays[0] == 1
    assert sum(delays[1:]) == pytest.approx(1.0)


def test_queue_observer_reconnects_and_resets_cursor_after_epoch_change(
    monkeypatch, capsys
) -> None:
    before = {"epoch": 0}
    after = {"epoch": 1}
    configs = iter((before, before, after))
    snapshots = iter(
        (
            {
                "state": "running",
                "run_id": "rewind-test",
                "entries": [{"record_id": 10, "author": "worker", "accepted": True, "body": "old"}],
            },
            {
                "state": "paused",
                "run_id": "rewind-test",
                "entries": [
                    {"record_id": 1, "author": "worker", "accepted": True, "body": "restored"}
                ],
            },
        )
    )
    monkeypatch.setattr(cli, "_load_config", lambda _workload: next(configs))
    monkeypatch.setattr(cli, "_queue_snapshot", lambda _config: next(snapshots))
    monkeypatch.setattr(cli.time, "sleep", lambda _seconds: None)
    arguments = cli.argparse.Namespace(workload="rewind", color="never", no_follow=False)
    assert cli.command_observe_queue(arguments) == 0
    output = capsys.readouterr().out
    assert output.count("SWARM JOURNAL") == 2
    assert output.index("JOURNAL #10") < output.rindex("JOURNAL #1")


def test_observer_reads_never_replace_the_live_journal(monkeypatch, tmp_path: Path) -> None:
    socket = tmp_path / "journal.sock"
    socket.touch()
    config = {
        "socket": str(socket),
        "database": str(tmp_path / "journal.db"),
        "run_id": "run",
        "run_dir": str(tmp_path),
    }
    monkeypatch.setattr(cli, "_workflow_client", lambda _config, journal: object())
    with pytest.raises(OSError, match="slow response"):
        cli._using_journal(config, lambda _client: (_ for _ in ()).throw(OSError("slow response")))

    class EmptyResponse:
        def __init__(self, _client) -> None:
            pass

        def _view(self, _run_id):
            raise json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(cli, "_workflow_client", lambda _config, journal: EmptyResponse(journal))
    assert cli._observer_context(config, "worker-0") == (
        "starting",
        ("gpt 5.6 sol medium", "-"),
    )


def test_controls_recover_a_stale_journal_socket(monkeypatch) -> None:
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        root = Path(directory)
        socket_path = root / "journal.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stale:
            stale.bind(str(socket_path))
        config = {
            "socket": str(socket_path),
            "database": str(root / "journal.db"),
            "journal_command": [],
            "min_exact": 5,
            "max_results": 10,
        }
        monkeypatch.setattr(
            cli, "_workflow_client", lambda _config, journal: cli.WorkflowClient(journal)
        )
        result = cli._using_journal(
            config,
            lambda client: client.journal.add("run", "launcher", "control: recovery-probe"),
        )
        assert result == {"saved": True, "record_id": 1}
        assert not socket_path.exists()


def test_macos_commands_and_controls_remain_available() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["verify"]).handler is cli.command_verify
    run = parser.parse_args(["harness", "run", "--workload", "web-app", "--target", "/tmp/product"])
    assert run.workers == 7
    assert run.reviews == 3
    assert (run.min_exact, run.max_results) == (5, 10)
    configured = parser.parse_args(
        [
            "harness",
            "run",
            "--workload",
            "web-app",
            "--target",
            "/tmp/product",
            "--reviews",
            "2",
        ]
    )
    assert configured.reviews == 2
    concurrency = parser.parse_args(["harness", "validate-concurrency"])
    assert concurrency.handler is cli.command_validate_concurrency
    assert concurrency.workers == 7
    assert concurrency.rounds == 6
    assert (concurrency.min_exact, concurrency.max_results) == (5, 10)
    for command in ("pause", "resume", "reset", "observe", "observe-queue", "status"):
        arguments = ["harness", command, "--workload", "web-app"]
        if command == "observe":
            arguments += ["--slot", "worker-0"]
        parsed = parser.parse_args(arguments)
        assert parsed.workload == "web-app"
    checkpoint = parser.parse_args(
        ["harness", "checkpoint", "--workload", "web-app", "--name", "productive"]
    )
    assert checkpoint.handler is cli.command_checkpoint
    rewind = parser.parse_args(["harness", "rewind", "--workload", "web-app", "--to", "productive"])
    assert rewind.handler is cli.command_rewind


def test_load_config_relocates_run_local_paths(monkeypatch, tmp_path: Path) -> None:
    root = tmp_path / "new-checkout"
    runs = root / ".swarm" / "runs"
    run_dir = runs / "web-app"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema_version": 7,
                "run_id": "web-app-20260808-120000",
                "workload": "web-app",
                "run_dir": "/old/checkout/.swarm/runs/web-app",
                "database": "/old/checkout/.swarm/runs/web-app/journal.sqlite3",
                "socket": "/old/checkout/.swarm/runs/web-app/journal.sock",
                "stage_path": "/old/target/swarm-harness/web-app.yaml",
                "target_repo": "/target/remains/unchanged",
                "target_branch": "main",
                "base_commit": "1" * 40,
                "git_identity": {"name": "Test", "email": "test@example.com"},
                "workers": 7,
                "required_reviews": 3,
                "journal_command": [],
                "concurrency_validation": {},
                "workload_ref": {"schema": "SwarmWorkloadRefV1"},
                "replay_root": "2" * 32,
                "epoch": 0,
                "history_sequence": 0,
                "epoch_frontiers": [],
                "min_exact": 5,
                "max_results": 10,
                "branch_prefix": "codex/swarm-web-app-20260808-120000",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli, "RUNS", runs)

    config = cli._load_config("web-app")

    assert config["run_dir"] == str(run_dir.resolve())
    assert config["database"] == str(run_dir.resolve() / "journal.sqlite3")
    assert config["socket"] == str(run_dir.resolve() / "journal.sock")
    assert config["stage_path"] == str(
        Path("/target/remains/unchanged/swarm-harness/web-app.yaml").resolve()
    )
    assert config["target_repo"] == "/target/remains/unchanged"

    tampered = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    tampered["branch_prefix"] = "refs/heads"
    (run_dir / "run.json").write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(cli.HarnessError, match="integrity"):
        cli._load_config("web-app")


def test_frozen_repository_workload_rejects_later_specification_commit(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "test@example.com"],
        check=True,
    )
    contract = target / "swarm-harness" / "web-app.yaml"
    contract.parent.mkdir()
    contract.write_text(_workload_text(), encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "add", "."], check=True)
    subprocess.run(["git", "-C", str(target), "commit", "-qm", "freeze workload"], check=True)
    base = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    reference = cli._frozen_workload_ref(target, base, "web-app")
    config = {"target_repo": str(target), "workload_ref": reference}
    assert cli._load_frozen_stage(config)["goal"].startswith("Build and verify")

    contract.write_text(_workload_text().replace("small web", "changed web"), encoding="utf-8")
    with pytest.raises(cli.HarnessError, match="start a new run"):
        cli._load_frozen_stage(config)
    subprocess.run(["git", "-C", str(target), "add", "."], check=True)
    subprocess.run(["git", "-C", str(target), "commit", "-qm", "change workload"], check=True)
    with pytest.raises(cli.HarnessError, match="start a new run"):
        cli._load_frozen_stage(config)


def test_destructive_rewind_restores_sequence_and_frontier_then_advances_epoch(
    monkeypatch, request, tmp_path: Path
) -> None:
    harness_root = Path(tempfile.mkdtemp(prefix="swr-", dir="/tmp"))
    request.addfinalizer(lambda: shutil.rmtree(harness_root, ignore_errors=True))
    runs = harness_root / ".swarm" / "runs"
    checkpoints = harness_root / ".swarm" / "checkpoints"
    run_dir = runs / "rewind"
    run_dir.mkdir(parents=True)
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "test@example.com"],
        check=True,
    )
    (target / "README.md").write_text("base\n", encoding="utf-8")
    contract = target / "swarm-harness" / "rewind.yaml"
    contract.parent.mkdir()
    contract.write_text(_workload_text(), encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "add", "."], check=True)
    subprocess.run(["git", "-C", str(target), "commit", "-qm", "base"], check=True)
    base = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    branch = subprocess.run(
        ["git", "-C", str(target), "symbolic-ref", "--short", "HEAD"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    blob = subprocess.run(
        ["git", "-C", str(target), "rev-parse", f"{base}:swarm-harness/rewind.yaml"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(target), "rev-parse", f"{base}:swarm-harness"],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    run_id = "rewind-20260809-120000"
    config = {
        "schema_version": 7,
        "run_id": run_id,
        "workload": "rewind",
        "target_repo": str(target),
        "target_branch": branch,
        "base_commit": base,
        "git_identity": {"name": "Test", "email": "test@example.com"},
        "run_dir": str(run_dir),
        "database": str(run_dir / "journal.sqlite3"),
        "socket": str(run_dir / "journal.sock"),
        "workers": 4,
        "required_reviews": 3,
        "journal_command": [],
        "min_exact": 1,
        "max_results": 2,
        "epoch": 0,
        "history_sequence": 0,
        "epoch_frontiers": [],
        "replay_root": "a" * 32,
        "workload_ref": {
            "schema": "SwarmWorkloadRefV1",
            "target_repository": str(target.resolve()),
            "base_commit": base,
            "workload_path": "swarm-harness/rewind.yaml",
            "workload_blob": blob,
            "harness_tree": tree,
            "harness_version": "test-harness",
        },
        "harness_version": "test-harness",
        "branch_prefix": f"codex/swarm-{run_id}",
        "concurrency_validation": {},
    }
    cli._atomic_json(run_dir / "run.json", config)
    assignments = run_dir / "assignments"
    assignments.mkdir()
    (assignments / "worker-0.txt").write_text("implementation\nepoch:0\n", encoding="utf-8")
    monkeypatch.setattr(cli, "ROOT", harness_root)
    monkeypatch.setattr(cli, "RUNS", runs)
    monkeypatch.setattr(cli, "CHECKPOINTS", checkpoints)
    monkeypatch.setattr(cli, "_run_processes", lambda _config: [])
    monkeypatch.setattr(cli, "_stop_processes", lambda _config: None)
    starts: list[int] = []
    monkeypatch.setattr(
        cli, "_start", lambda restored, _foreground: starts.append(int(restored["epoch"])) or 0
    )

    runtime = start_journal(
        config["database"],
        config["socket"],
        min_exact=1,
        max_results=2,
    )
    client = cli._workflow_client(config, runtime.client)
    client.bootstrap(run_id)
    client.add(run_id, "worker-0", "claim: task:API")
    runtime.close()
    checkpoint = cli._snapshot_checkpoint(config, "productive")
    manifest = json.loads((checkpoint / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["journal_sequence"] == 2

    runtime = start_journal(
        config["database"],
        config["socket"],
        min_exact=1,
        max_results=2,
    )
    discarded = cli._workflow_client(config, runtime.client)
    discarded.add(run_id, "worker-0", "checkpoint: task:API\ndiscard me")
    runtime.close()
    (target / "README.md").write_text("discarded frontier\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(target), "commit", "-qm", "discarded"], check=True)

    arguments = cli.argparse.Namespace(
        workload="rewind", to="productive", archive=False, foreground=False
    )
    local_change = target / "operator-notes.txt"
    local_change.write_text("do not erase\n", encoding="utf-8")
    with pytest.raises(cli.HarnessError, match="refuses to overwrite"):
        cli.command_rewind(arguments)
    local_change.unlink()
    assert cli.command_rewind(arguments) == 0
    restored = cli._load_config("rewind")
    assert restored["epoch"] == 1
    assert restored["history_sequence"] == 2
    assert restored["epoch_frontiers"] == [{"epoch": 0, "through": 2}]
    assert starts == [1]
    assert (assignments / "worker-0.txt").read_text(encoding="utf-8").splitlines() == [
        "implementation",
        "epoch:1",
    ]
    assert (target / "README.md").read_text(encoding="utf-8") == "base\n"

    runtime = start_journal(
        restored["database"],
        restored["socket"],
        min_exact=1,
        max_results=2,
    )
    try:
        current = cli._workflow_client(restored, runtime.client)
        replayed = current.replay_records(run_id)
        assert [item["journal_sequence"] for item in replayed] == [1, 2, 3]
        assert "discard me" not in "\n".join(item["text"] for item in replayed)
        assert replayed[-1]["record_id"] == 3
        stale = WorkflowClient(
            runtime.client,
            cli._load_frozen_stage(restored),
            restored["workload_ref"],
            replay_root=restored["replay_root"],
            epoch=0,
            history_sequence=2,
            serialization_path=run_dir / "workflow.lock",
        )
        for stale_text in ("claim: task:UI", "open-pr: task:API\nstale terminal result"):
            with pytest.raises(WorkflowError, match="epoch is stale"):
                stale.add(run_id, "worker-1", stale_text)
        assert len(current.replay_records(run_id)) == 3
    finally:
        runtime.close()


def test_every_workload_is_blocked_before_staging_without_a_qualified_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=target, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=target, check=True)
    contract = target / "swarm-harness" / "web-app.yaml"
    contract.parent.mkdir()
    contract.write_text(_workload_text(), encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(["git", "commit", "-qm", "contract"], cwd=target, check=True)
    arguments = cli.build_parser().parse_args(
        [
            "harness",
            "run",
            "--workload",
            "web-app",
            "--target",
            str(target),
            "--workers",
            "7",
        ]
    )
    monkeypatch.setattr(cli, "_config_path", lambda _: tmp_path / "run.json")

    def reject(
        _root: Path,
        _receipt: Path,
        *,
        required_workers: int,
        journal_command=None,
        min_exact: int,
        max_results: int,
    ):
        assert required_workers == 7
        assert journal_command is None
        assert (min_exact, max_results) == (5, 10)
        raise ConcurrencyError("qualification required")

    monkeypatch.setattr(cli, "validate_receipt", reject)
    with pytest.raises(ConcurrencyError, match="qualification required"):
        cli.command_run(arguments)
    assert not (tmp_path / "run.json").exists()


def test_native_launcher_worker_mcp_and_git_complete_generic_workload(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    subprocess.run(["git", "-C", str(target), "config", "user.name", "Test"], check=True)
    subprocess.run(
        ["git", "-C", str(target), "config", "user.email", "test@example.com"], check=True
    )
    (target / "README.md").write_text("fixture\n", encoding="utf-8")
    contract = target / "swarm-harness" / "smoke.yaml"
    contract.parent.mkdir()
    contract.write_text(
        (ROOT / "tests" / "fixtures" / "workloads" / "smoke.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(target), "add", "README.md", "swarm-harness"], check=True)
    subprocess.run(["git", "-C", str(target), "commit", "-qm", "seed"], check=True)
    receipt = _write_receipt(tmp_path / "validation", workers=4)

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
            "--concurrency-receipt",
            str(receipt),
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
    assert config["schema_version"] == 7
    assert (config["min_exact"], config["max_results"]) == (5, 10)
    assert config["epoch"] == 0
    workload_ref = config["workload_ref"]
    assert workload_ref["target_repository"] == str(target.resolve())
    assert workload_ref["base_commit"] == config["base_commit"]
    assert workload_ref["workload_path"] == "swarm-harness/smoke.yaml"
    assert (
        workload_ref["workload_blob"]
        == subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "rev-parse",
                f"{config['base_commit']}:swarm-harness/smoke.yaml",
            ],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )
    assert (
        workload_ref["harness_tree"]
        == subprocess.run(
            ["git", "-C", str(target), "rev-parse", f"{config['base_commit']}:swarm-harness"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
    )
    assert workload_ref["harness_version"] == config["harness_version"]
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
    contract_diff = subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "diff",
            "--exit-code",
            config["base_commit"],
            target_tip,
            "--",
            "swarm-harness",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert contract_diff.returncode == 0, contract_diff.stdout + contract_diff.stderr
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
