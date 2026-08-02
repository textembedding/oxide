from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from swarm_harness.cli import highlight_stream_line
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
    assert tool_names == ["journal_add", "journal_search"]


def test_worker_claim_binding_is_forwarded_only_to_required_mcp() -> None:
    worker = Worker(JournalClient("/tmp/not-used.sock"), "run", "worker-0")
    configs = worker._mcp_configs()
    assert 'shell_environment_policy.exclude=["SWARM_*"]' in configs
    assert "mcp_servers.journal.required=true" in configs
    assert 'mcp_servers.journal.enabled_tools=["journal_add","journal_search"]' in configs


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
        "import json,os,pathlib,sys\n"
        "from swarm_harness.journal_client import JournalClient\n"
        "client=JournalClient(os.environ['SWARM_JOURNAL_SOCKET'])\n"
        "binding={'run_id':os.environ['SWARM_RUN_ID'],'worker_id':os.environ['SWARM_WORKER_ID'],"
        "'task_id':os.environ['SWARM_TASK_ID'],'claim_token':os.environ['SWARM_CLAIM_TOKEN']}\n"
        "task=binding['task_id']\n"
        "client.call('journal_search',**binding,query='task:'+task)\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'mcp_tool_call','server':'journal','tool':'journal_search','status':'completed'}}))\n"
        "root=pathlib.Path(sys.argv[sys.argv.index('-C')+1])\n"
        "(root/'result.txt').write_text('done\\n')\n"
        "client.call('journal_add',**binding,text='checkpoint: task:'+task+'\\nstate: durable')\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'mcp_tool_call','server':'journal','tool':'journal_add','status':'completed'}}))\n"
        "client.call('journal_add',**binding,text='handoff: task:'+task+'\\nstate: complete')\n"
        "print(json.dumps({'type':'item.completed','item':{'type':'mcp_tool_call','server':'journal','tool':'journal_add','status':'completed'}}))\n"
        'print(\'{"type":"result","text":"done"}\')\n',
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
        logs: list[str] = []
        worker = Worker(
            client,
            "run",
            "worker-0",
            codex_argv=(sys.executable, str(fake)),
            log=logs.append,
        )
        assert worker.run_once() == "submitted"
        assert sum('"tool": "journal_search"' in line for line in logs) == 1
        assert sum('"tool": "journal_add"' in line for line in logs) == 2
        rendered = "\n".join(highlight_stream_line(line, color=False) for line in logs)
        assert "TOOL COMPLETED journal.journal_search" in rendered
        assert "TOOL COMPLETED journal.journal_add" in rendered
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
