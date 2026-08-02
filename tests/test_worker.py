from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from swarm_harness.journal_client import JournalClient
from swarm_harness.sqlite_service import JournalServer, SQLiteJournal
from swarm_harness.tools import __all__ as tool_names
from swarm_harness.worker import Worker


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def test_exactly_two_harness_tools_are_exported() -> None:
    assert tool_names == ["claim_task", "submit_result"]


def test_real_worker_adapter_commits_and_submits(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    git(worktree, "init", "-b", "main")
    git(worktree, "config", "user.name", "Worker Test")
    git(worktree, "config", "user.email", "worker@example.invalid")
    (worktree / "README.md").write_text("seed\n", encoding="utf-8")
    git(worktree, "add", "README.md")
    git(worktree, "commit", "-m", "seed")
    fake = tmp_path / "fake_codex.py"
    fake.write_text(
        "import pathlib,sys\n"
        "root=pathlib.Path(sys.argv[sys.argv.index('-C')+1])\n"
        "(root/'result.txt').write_text('done\\n')\n"
        "print('{\"type\":\"result\",\"text\":\"done\"}')\n",
        encoding="utf-8",
    )
    socket_root = tempfile.TemporaryDirectory(prefix="swarm-w-", dir="/tmp")
    socket_path = Path(socket_root.name) / "journal.sock"
    server = JournalServer(socket_path, SQLiteJournal(tmp_path / "journal.db"))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = JournalClient(socket_path)
        client.call(
            "create_run",
            run_id="run",
            workload="test",
            target_repo=str(worktree),
            integration_branch="main",
            integration_worktree=str(worktree),
            tasks=[
                {
                    "id": "TASK",
                    "title": "Task",
                    "prompt": "Create result.txt",
                    "depends_on": [],
                    "checks": ["test -f result.txt"],
                }
            ],
        )
        client.call(
            "prepare_task",
            run_id="run",
            task_id="TASK",
            branch="main",
            worktree_path=str(worktree),
        )
        worker = Worker(
            client,
            "run",
            "worker-0",
            codex_argv=(sys.executable, str(fake)),
            log=lambda _: None,
        )
        assert worker.run_once() == "submitted"
        submitted = client.call("submitted_tasks", run_id="run")
        assert submitted[0]["submission"]["outcome"] == "completed"
        assert (worktree / "result.txt").read_text() == "done\n"
        assert git(worktree, "status", "--porcelain=v1") == ""
        for validator in ("worker-1", "worker-2"):
            reviewer = Worker(
                client,
                "run",
                validator,
                codex_argv=(sys.executable, str(fake)),
                log=lambda _: None,
            )
            assert reviewer.run_once() == "validated"
        proposal = client.run_status("run")["proposals"][0]
        assert proposal["state"] == "committed"
        assert proposal["approvals"] == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        socket_root.cleanup()
