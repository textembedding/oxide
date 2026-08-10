from __future__ import annotations

import secrets
from pathlib import Path

from oxide.journal import JournalClient, serve_in_thread
from oxide.journal_mcp import PROTOCOL_VERSION, JournalMcpServer
from oxide.workflow import WorkflowClient

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
    socket = Path("/tmp") / f"oxide-test-{secrets.token_hex(8)}.sock"
    service, thread = serve_in_thread(tmp_path / "journal.sqlite3", socket)
    client = WorkflowClient(JournalClient(socket), STAGE)
    client.bootstrap("run")
    server = JournalMcpServer(client, "run", "worker-0")

    initialized = _request(
        server,
        1,
        "initialize",
        {"protocolVersion": PROTOCOL_VERSION},
    )
    assert initialized["result"]["serverInfo"]["name"] == "oxide-journal"
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
    description = search["description"]
    assert "bounded union" in description
    assert "threshold-eligible semantic" in description
    assert "exact-match floor" in description
    assert "ordered only by journal sequence" in description
    assert "iterative follow-up searches" in description

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


def test_mcp_keeps_semantic_extras_visible_for_multi_hop_search(tmp_path: Path) -> None:
    socket = Path("/tmp") / f"oxide-test-{secrets.token_hex(8)}.sock"
    service, thread = serve_in_thread(tmp_path / "journal.sqlite3", socket)
    try:
        client = WorkflowClient(JournalClient(socket), STAGE)
        client.bootstrap("run")
        client.add("run", "worker-1", "discovery: component-A implicated failure-Z")
        client.add("run", "worker-2", "decision: failure-Z repair is retry-safe")
        server = JournalMcpServer(client, "run", "worker-0")

        first = _request(
            server,
            1,
            "tools/call",
            {
                "name": "journal_search",
                "arguments": {"yaml": "query: component-A failure-Z"},
            },
        )["result"]["content"][0]["text"]
        assert 'match_kind: "semantic"' in first
        assert "component-A implicated failure-Z" in first

        second = _request(
            server,
            2,
            "tools/call",
            {"name": "journal_search", "arguments": {"yaml": "query: failure-Z"}},
        )["result"]["content"][0]["text"]
        assert second.count('match_kind: "exact"') == 2
        assert "failure-Z repair is retry-safe" in second
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=5)
