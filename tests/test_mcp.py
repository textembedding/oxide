from __future__ import annotations

import json
import secrets
from pathlib import Path

from swarm_harness.journal import JournalClient, serve_in_thread
from swarm_harness.journal_mcp import PROTOCOL_VERSION, JournalMcpServer
from swarm_harness.workflow import WorkflowClient

STAGE = {
    "required_reviews": 3,
    "tasks": [
        {
            "id": "A",
            "title": "A",
            "prompt": "implement A",
            "depends_on": [],
            "checks": ["test A"],
        }
    ],
}


def _request(server: JournalMcpServer, number: int, method: str, params: dict) -> dict:
    response = server.handle({"jsonrpc": "2.0", "id": number, "method": method, "params": params})
    assert response is not None
    return response


def test_mcp_exposes_only_add_and_search(monkeypatch, tmp_path: Path) -> None:
    socket = Path("/tmp") / f"swarm-test-{secrets.token_hex(8)}.sock"
    service, thread = serve_in_thread(tmp_path / "journal.sqlite3", socket)
    client = WorkflowClient(JournalClient(socket))
    client.add(
        "run",
        "launcher",
        "bootstrap: run:run\nstage-json: " + json.dumps(STAGE, separators=(",", ":")),
    )
    monkeypatch.setenv("SWARM_JOURNAL_SOCKET", str(socket))
    monkeypatch.setenv("SWARM_RUN_ID", "run")
    monkeypatch.setenv("SWARM_WORKER_ID", "worker-0")
    server = JournalMcpServer()

    initialized = _request(
        server,
        1,
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION},
    )
    assert initialized["result"]["serverInfo"]["name"] == "swarm-journal"
    listed = _request(server, 2, "tools/list", {})
    assert [tool["name"] for tool in listed["result"]["tools"]] == [
        "journal_add",
        "journal_search",
    ]
    add, search = listed["result"]["tools"]
    assert add["annotations"] == {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    assert search["annotations"]["readOnlyHint"] is True
    assert search["annotations"]["openWorldHint"] is False

    searched = _request(
        server,
        3,
        "tools/call",
        {"name": "journal_search", "arguments": {"yaml": "query: queue:ready"}},
    )
    assert 'task_id: "A"' in searched["result"]["content"][0]["text"]
    added = _request(
        server,
        4,
        "tools/call",
        {"name": "journal_add", "arguments": {"yaml": "text: claim: task:A"}},
    )
    assert 'claim: "accepted"' in added["result"]["content"][0]["text"]
    unknown = _request(
        server,
        5,
        "tools/call",
        {"name": "claim_task", "arguments": {"yaml": "text: nope"}},
    )
    assert unknown["result"]["isError"] is True
    service.shutdown()
    service.server_close()
    thread.join(timeout=5)
