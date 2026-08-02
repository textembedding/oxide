from __future__ import annotations

import tempfile
import threading
from pathlib import Path

import pytest

from swarm_harness.journal_client import JournalClient
from swarm_harness.protocol import JournalError
from swarm_harness.sqlite_service import JournalServer, SQLiteJournal


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def journal(tmp_path: Path):
    clock = Clock()
    socket_root = tempfile.TemporaryDirectory(prefix="swarm-j-", dir="/tmp")
    socket_path = Path(socket_root.name) / "journal.sock"
    server = JournalServer(socket_path, SQLiteJournal(tmp_path / "journal.db", clock))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = JournalClient(socket_path)
    yield client, clock, socket_path
    server.shutdown()
    server.server_close()
    thread.join()
    socket_root.cleanup()


def seed(client: JournalClient, tasks: list[dict] | None = None) -> None:
    client.call(
        "create_run",
        run_id="run",
        workload="toy",
        target_repo="/target",
        integration_branch="codex/test",
        integration_worktree="/integration",
        tasks=tasks
        or [
            {
                "id": "A",
                "title": "A",
                "prompt": "Do A",
                "depends_on": [],
                "checks": ["true"],
            }
        ],
    )


def prepare(client: JournalClient, task_id: str = "A") -> None:
    client.call(
        "prepare_task",
        run_id="run",
        task_id=task_id,
        branch=f"codex/{task_id}",
        worktree_path=f"/worktrees/{task_id}",
    )


def test_concurrent_workers_cannot_claim_the_same_task(journal) -> None:
    client, _, socket_path = journal
    seed(client)
    prepare(client)
    barrier = threading.Barrier(3)
    results: list[dict] = []

    def claim(worker: str) -> None:
        own = JournalClient(socket_path)
        barrier.wait()
        results.append(own.claim_task("run", worker, 60))

    threads = [threading.Thread(target=claim, args=(f"worker-{index}",)) for index in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert sorted(item["status"] for item in results) == ["claimed", "idle"]


def test_stale_token_rejects_and_valid_submission_survives_restart(journal) -> None:
    client, _, _ = journal
    seed(client)
    prepare(client)
    claim = client.claim_task("run", "worker", 60)
    with pytest.raises(JournalError, match="stale"):
        client.submit_result(
            run_id="run",
            task_id="A",
            claim_token="incorrect",
            outcome="completed",
            summary="bad",
            commit_sha="a" * 40,
            blockers=[],
            proposed_followups=[],
        )
    client.submit_result(
        run_id="run",
        task_id="A",
        claim_token=claim["claim_token"],
        outcome="completed",
        summary="done",
        commit_sha="a" * 40,
        blockers=[],
        proposed_followups=[{"title": "not executable"}],
    )
    assert client.run_status("run")["tasks"][0]["state"] == "submitted"
    status = client.run_status("run")
    assert status["proposed_followups"] == [
        {"task_id": "A", "proposal": {"title": "not executable"}}
    ]
    assert len(status["tasks"]) == 1


def test_expired_lease_is_reclaimed_and_old_token_stays_invalid(journal) -> None:
    client, clock, _ = journal
    seed(client)
    prepare(client)
    old = client.claim_task("run", "old-worker", 5)
    clock.now += 6
    assert client.call("runnable_unprepared", run_id="run")[0]["task_id"] == "A"
    prepare(client)
    new = client.claim_task("run", "new-worker", 5)
    assert new["claim_token"] != old["claim_token"]
    with pytest.raises(JournalError, match="stale"):
        client.submit_result(
            run_id="run",
            task_id="A",
            claim_token=old["claim_token"],
            outcome="completed",
            summary="late",
            commit_sha="a" * 40,
            blockers=[],
            proposed_followups=[],
        )


def test_dependencies_hold_downstream_until_acceptance(journal) -> None:
    client, _, _ = journal
    seed(
        client,
        [
            {"id": "A", "title": "A", "prompt": "A", "depends_on": [], "checks": []},
            {"id": "B", "title": "B", "prompt": "B", "depends_on": ["A"], "checks": []},
        ],
    )
    assert [row["task_id"] for row in client.call("runnable_unprepared", run_id="run")] == [
        "A"
    ]
    prepare(client, "A")
    claim = client.claim_task("run", "worker", 60)
    client.submit_result(
        run_id="run",
        task_id="A",
        claim_token=claim["claim_token"],
        outcome="completed",
        summary="done",
        commit_sha="a" * 40,
        blockers=[],
        proposed_followups=[],
    )
    assert client.call("runnable_unprepared", run_id="run") == []
    client.call("accept_task", run_id="run", task_id="A", commit_sha="b" * 40)
    assert [row["task_id"] for row in client.call("runnable_unprepared", run_id="run")] == [
        "B"
    ]


def test_pause_fences_claim_and_resume_preserves_prepared_worktree(journal) -> None:
    client, _, _ = journal
    seed(client)
    prepare(client)
    original = client.claim_task("run", "worker-0", 60)

    paused = client.call("pause_run", run_id="run")

    assert paused == {"state": "paused", "paused_claims": 1}
    status = client.run_status("run")
    assert status["run"]["state"] == "paused"
    assert status["tasks"][0]["state"] == "pending"
    assert status["tasks"][0]["worktree_path"] == "/worktrees/A"
    assert client.claim_task("run", "worker-1", 60) == {"status": "paused"}
    with pytest.raises(JournalError, match="stale"):
        client.submit_result(
            run_id="run",
            task_id="A",
            claim_token=original["claim_token"],
            outcome="completed",
            summary="late",
            commit_sha="a" * 40,
            blockers=[],
            proposed_followups=[],
        )

    assert client.call("resume_run", run_id="run") == {"state": "running"}
    resumed = client.claim_task("run", "worker-1", 60)
    assert resumed["status"] == "claimed"
    assert resumed["task_id"] == "A"
    assert resumed["worktree_path"] == "/worktrees/A"
    assert resumed["claim_token"] != original["claim_token"]
