from __future__ import annotations

import json
import sys
from pathlib import Path

from swarm_harness.worker import Worker


class SearchOnlyClient:
    socket_path = "/tmp/journal.sock"

    def __init__(self) -> None:
        self.queries: list[str] = []

    def search(self, run_id: str, query: str) -> list[dict]:
        assert run_id == "run"
        self.queries.append(query)
        if query == "run:state":
            return [{"state": "running"}]
        if query == "worker:worker-0":
            return [{"task_id": "A", "state": "working"}]
        if query == "queue:ready":
            return []
        raise AssertionError(query)


class ClaimClient(SearchOnlyClient):
    def __init__(self) -> None:
        super().__init__()
        self.claims: list[str] = []

    def search(self, run_id: str, query: str) -> list[dict]:
        if query == "worker:worker-1":
            return []
        if query == "queue:ready":
            return [{"claim": f"claim: task:{task}"} for task in ("A", "B", "C")]
        return super().search(run_id, query)

    def add(self, run_id: str, worker: str, text: str) -> dict:
        assert (run_id, worker) == ("run", "worker-1")
        self.claims.append(text)
        return {"claim": "accepted"}


def test_worker_host_uses_only_search_and_codex_gets_exact_two_tools(
    monkeypatch, tmp_path: Path
) -> None:
    capture = tmp_path / "capture.json"
    fake = tmp_path / "fake_codex.py"
    fake.write_text(
        """import json, os, sys
from pathlib import Path
Path(os.environ['CAPTURE']).write_text(json.dumps({'argv': sys.argv[1:], 'env': {key: value for key, value in os.environ.items() if key.startswith('SWARM_')}}))
print(json.dumps({'type':'turn.completed'}))
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("CAPTURE", str(capture))
    client = SearchOnlyClient()
    logs: list[str] = []
    worker = Worker(
        client,  # type: ignore[arg-type]
        "run",
        "worker-0",
        tmp_path,
        "main",
        tmp_path,
        codex_argv=(sys.executable, str(fake)),
        log=logs.append,
    )
    assert worker.run_once() == "worked"
    record = json.loads(capture.read_text(encoding="utf-8"))
    joined = " ".join(record["argv"])
    assert 'enabled_tools=["journal_add","journal_search"]' in joined
    assert "journal_mcp" in joined
    assert str((tmp_path / ".git").resolve()) in joined
    assert set(record["env"]) == {
        "SWARM_JOURNAL_SOCKET",
        "SWARM_RUN_ID",
        "SWARM_WORKER_ID",
    }
    prompt = record["argv"][-1]
    assert "queue:ready" in prompt
    assert "rotate that list left by 0" in prompt
    assert "host normally preclaims" in prompt
    assert "Prior-generation\n  authorship is not" in prompt
    assert "INTERNAL REVIEW" in prompt
    assert "configured approval count" in prompt
    assert "There is no integration branch" in prompt
    assert "lease" not in prompt.lower()
    assert client.queries == ["run:state", "worker:worker-0", "queue:ready"]
    assert any("turn.completed" in line for line in logs)


def test_worker_claims_rotated_ready_work_before_model_launch(monkeypatch, tmp_path: Path) -> None:
    client = ClaimClient()
    worker = Worker(client, "run", "worker-1", tmp_path, "main", tmp_path)  # type: ignore[arg-type]
    monkeypatch.setattr(worker, "_codex", lambda: 0)
    assert worker.run_once() == "worked"
    assert client.claims == ["claim: task:B"]


def test_worker_detects_accepted_terminal_journal_record() -> None:
    event = {
        "item": {
            "type": "mcp_tool_call",
            "server": "journal",
            "tool": "journal_add",
            "status": "completed",
            "arguments": {"yaml": "text: 'approve: review:A:1:1'"},
            "result": {"content": [{"text": "saved: true"}]},
        }
    }
    assert Worker._terminal_record(json.dumps(event))
    event["item"]["arguments"] = {"yaml": "text: 'checkpoint: task:A'"}
    assert not Worker._terminal_record(json.dumps(event))
