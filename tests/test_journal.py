from __future__ import annotations

import sqlite3
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
    assert client.call(
        "reclaim_worker",
        run_id="run",
        worker_id="worker",
        reason="worker_process_exited_after_submission",
    ) == {"run_state": "running", "reclaimed": []}
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


def test_observable_owner_has_no_expiry_and_is_reclaimed_by_process_loss(journal) -> None:
    client, clock, _ = journal
    seed(client)
    prepare(client)
    original = client.claim_task("run", "worker-0")

    assert original["ownership_mode"] == "observable"
    assert original["lease_expires_at"] is None
    clock.now += 1_000_000
    assert client.call("runnable_unprepared", run_id="run") == []

    reclaimed = client.call(
        "reclaim_worker",
        run_id="run",
        worker_id="worker-0",
        reason="worker_process_exited",
    )
    assert reclaimed == {
        "run_state": "running",
        "reclaimed": [{"task_id": "A", "worktree_path": "/worktrees/A"}],
    }
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
    replacement = client.claim_task("run", "worker-1")
    assert replacement["status"] == "claimed"
    assert replacement["worktree_path"] == "/worktrees/A"


def test_legacy_fixed_lease_client_is_retired_before_another_claim(journal) -> None:
    client, _, _ = journal
    seed(client)
    prepare(client)

    assert client.call(
        "claim_task",
        run_id="run",
        worker_id="legacy-worker",
        lease_seconds=3600,
    ) == {"status": "stopped"}
    assert client.run_status("run")["tasks"][0]["state"] == "pending"


def test_legacy_fixed_leases_migrate_to_observable_ownership(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE claims (
              claim_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              task_id TEXT NOT NULL, worker_id TEXT NOT NULL,
              token TEXT NOT NULL UNIQUE, claimed_at REAL NOT NULL,
              expires_at REAL NOT NULL, state TEXT NOT NULL, submission_json TEXT
            )
            """
        )
        connection.execute(
            "INSERT INTO claims(run_id,task_id,worker_id,token,claimed_at,expires_at,state) VALUES(?,?,?,?,?,?,?)",
            ("run", "A", "worker-0", "token", 10.0, 3610.0, "active"),
        )

    SQLiteJournal(database)

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT ownership_mode,expires_at,state FROM claims"
        ).fetchone()
    assert row == ("observable", None, "active")


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
    submission = client.submit_result(
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
    for validator in ("validator-0", "validator-1"):
        validation = client.claim_work("run", validator, 60)
        client.submit_validation(
            run_id="run",
            worker_id=validator,
            proposal_id=validation["proposal_id"],
            claim_token=validation["claim_token"],
            vote="approve",
            evidence={"checked": True},
        )
    client.call(
        "apply_proposal",
        run_id="run",
        proposal_id=submission["proposal_id"],
        success=True,
        integration_commit="b" * 40,
    )
    assert [row["task_id"] for row in client.call("runnable_unprepared", run_id="run")] == [
        "B"
    ]


def test_candidate_author_is_excluded_and_two_independent_votes_are_required(journal) -> None:
    client, _, _ = journal
    seed(client)
    prepare(client)
    claim = client.claim_task("run", "author")
    submission = client.submit_result(
        run_id="run",
        task_id="A",
        claim_token=claim["claim_token"],
        outcome="completed",
        summary="candidate",
        commit_sha="a" * 40,
        blockers=[],
        proposed_followups=[],
    )

    assert client.claim_work("run", "author")["status"] == "idle"
    first = client.claim_work("run", "validator-0")
    result = client.submit_validation(
        run_id="run",
        worker_id="validator-0",
        proposal_id=first["proposal_id"],
        claim_token=first["claim_token"],
        vote="approve",
        evidence={"check": "one"},
    )
    assert result["state"] == "open"
    assert client.run_status("run")["tasks"][0]["state"] == "submitted"
    assert client.claim_work("run", "validator-0")["status"] == "idle"

    second = client.claim_work("run", "validator-1")
    result = client.submit_validation(
        run_id="run",
        worker_id="validator-1",
        proposal_id=second["proposal_id"],
        claim_token=second["claim_token"],
        vote="approve",
        evidence={"check": "two"},
    )
    assert result == {
        "recorded": True,
        "state": "committed",
        "decision": "approve",
        "approvals": 2,
        "rejections": 0,
    }
    assert client.run_status("run")["tasks"][0]["state"] == "integrating"
    with pytest.raises(JournalError, match="quorum"):
        client.call("accept_task", run_id="run", task_id="A", commit_sha="b" * 40)
    assert submission["proposal_id"] == second["proposal_id"]


def test_rejection_quorum_retries_candidate_without_launcher_authority(journal) -> None:
    client, _, _ = journal
    seed(client)
    prepare(client)
    claim = client.claim_task("run", "author")
    client.submit_result(
        run_id="run",
        task_id="A",
        claim_token=claim["claim_token"],
        outcome="completed",
        summary="candidate",
        commit_sha="a" * 40,
        blockers=[],
        proposed_followups=[],
    )
    for validator in ("validator-0", "validator-1"):
        validation = client.claim_work("run", validator)
        result = client.submit_validation(
            run_id="run",
            worker_id=validator,
            proposal_id=validation["proposal_id"],
            claim_token=validation["claim_token"],
            vote="reject",
            evidence={"reason": "candidate check failed"},
        )
    assert result["decision"] == "reject"
    task = client.run_status("run")["tasks"][0]
    assert task["state"] == "pending"
    assert task["worktree_path"] is None
    assert task["last_error"] == "candidate rejected by independent validation quorum"


def test_permissionless_decomposition_is_applied_only_after_quorum(journal) -> None:
    client, _, _ = journal
    seed(client)
    proposal = client.call(
        "propose_change",
        run_id="run",
        worker_id="worker-0",
        kind="task_decomposition",
        payload={
            "tasks": [
                {
                    "id": "B",
                    "title": "B",
                    "prompt": "Do B",
                    "depends_on": ["A"],
                    "checks": ["true"],
                }
            ]
        },
    )
    assert len(client.run_status("run")["tasks"]) == 1
    for validator in ("worker-1", "worker-2"):
        validation = client.claim_work("run", validator)
        client.submit_validation(
            run_id="run",
            worker_id=validator,
            proposal_id=validation["proposal_id"],
            claim_token=validation["claim_token"],
            vote="approve",
            evidence={"schema": "checked"},
        )
    status = client.run_status("run")
    assert proposal["proposal_id"] == status["proposals"][0]["proposal_id"]
    assert [task["task_id"] for task in status["tasks"]] == ["A", "B"]


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
