from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from swarm_harness.journal_client import JournalClient
from swarm_harness.journal_mcp import PROTOCOL_VERSION, JournalMcpServer
from swarm_harness.sqlite_service import JournalServer, SQLiteJournal


def test_mcp_exposes_only_fenced_journal_add_and_search(tmp_path: Path, monkeypatch) -> None:
    socket_root = tempfile.TemporaryDirectory(prefix="swarm-mcp-", dir="/tmp")
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
            target_repo="/target",
            integration_branch="main",
            integration_worktree="/integration",
            tasks=[
                {
                    "id": "TASK",
                    "title": "Task",
                    "prompt": "Implement task",
                    "depends_on": [],
                    "checks": [],
                }
            ],
        )
        client.call(
            "prepare_task",
            run_id="run",
            task_id="TASK",
            branch="codex/task",
            worktree_path="/worktree",
        )
        claim = client.claim_task("run", "worker-0")
        environment = {
            "SWARM_JOURNAL_SOCKET": str(socket_path),
            "SWARM_RUN_ID": "run",
            "SWARM_WORKER_ID": "worker-0",
            "SWARM_TASK_ID": "TASK",
            "SWARM_CLAIM_TOKEN": claim["claim_token"],
        }
        for key, value in environment.items():
            monkeypatch.setenv(key, value)
        mcp = JournalMcpServer()
        initialized = mcp.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION},
            }
        )
        assert initialized["result"]["protocolVersion"] == PROTOCOL_VERSION
        listed = mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert [tool["name"] for tool in listed["result"]["tools"]] == [
            "journal_add",
            "journal_search",
        ]

        def call(tool: str, yaml: str, request_id: int) -> dict:
            response = mcp.handle(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": {"yaml": yaml}},
                }
            )
            assert response["result"]["isError"] is False
            return response

        searched = call("journal_search", "query: task:TASK", 3)
        assert "task:TASK" in searched["result"]["content"][0]["text"]
        call(
            "journal_add",
            "text: |\n  checkpoint: task:TASK\n  state: durable",
            4,
        )
        call(
            "journal_add",
            "text: |\n  handoff: task:TASK\n  state: complete",
            5,
        )
        assert (
            client.submit_result(
                run_id="run",
                task_id="TASK",
                claim_token=claim["claim_token"],
                outcome="completed",
                summary="done",
                commit_sha="a" * 40,
                blockers=[],
                proposed_followups=[],
            )["recorded"]
            is True
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        socket_root.cleanup()
