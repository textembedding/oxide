from __future__ import annotations

import subprocess
import tempfile
import threading
from pathlib import Path

from swarm_harness.controller import Controller, load_stage
from swarm_harness.journal_client import JournalClient
from swarm_harness.sqlite_service import JournalServer, SQLiteJournal

ROOT = Path(__file__).resolve().parents[1]


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def commit_task(worktree: Path, task_id: str) -> str:
    output = worktree / "toy-output"
    output.mkdir(exist_ok=True)
    if task_id == "TOY-01":
        (output / "one.txt").write_text("one\n", encoding="utf-8")
    elif task_id == "TOY-02":
        (output / "two.txt").write_text("two\n", encoding="utf-8")
    else:
        (output / "combined.txt").write_text("one\ntwo\n", encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", f"Complete {task_id}")
    return git(worktree, "rev-parse", "HEAD")


def test_toy_stage_completes_claim_verify_and_merge(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    git(target, "init", "-b", "main")
    git(target, "config", "user.name", "Harness Test")
    git(target, "config", "user.email", "harness@example.invalid")
    (target / "README.md").write_text("target\n", encoding="utf-8")
    git(target, "add", "README.md")
    git(target, "commit", "-m", "seed")
    socket_root = tempfile.TemporaryDirectory(prefix="swarm-c-", dir="/tmp")
    socket_path = Path(socket_root.name) / "journal.sock"
    journal = SQLiteJournal(tmp_path / "journal.db")
    server = JournalServer(socket_path, journal)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = JournalClient(socket_path)
        stage = load_stage(ROOT / "stages" / "toy.yaml")
        controller = Controller(client, "toy-test", "toy", stage, target, tmp_path / "run")
        controller.seed()
        for expected in ("TOY-01", "TOY-02", "TOY-03"):
            controller.prepare_runnable()
            claim = client.claim_task("toy-test", "fake-worker", 60)
            assert claim["task_id"] == expected
            worktree = Path(claim["worktree_path"])
            commit = commit_task(worktree, expected)
            client.submit_result(
                run_id="toy-test",
                task_id=expected,
                claim_token=claim["claim_token"],
                outcome="completed",
                summary="done",
                commit_sha=commit,
                blockers=[],
                proposed_followups=[],
            )
            controller.tick()
        assert controller.tick() == "complete"
        assert (controller.integration / "toy-output" / "combined.txt").read_text() == "one\ntwo\n"
        assert all(task["state"] == "accepted" for task in client.run_status("toy-test")["tasks"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        socket_root.cleanup()
