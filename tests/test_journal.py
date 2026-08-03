from __future__ import annotations

import copy
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from swarm_harness.cli import load_stage
from swarm_harness.journal import Journal, JournalClient, JournalError, serve_in_thread
from swarm_harness.workflow import WorkflowClient, WorkflowError


def _stage() -> dict:
    return {
        "stage": 0,
        "enabled": True,
        "goal": "test",
        "required_reviews": 3,
        "tasks": [
            {
                "id": "A",
                "title": "first",
                "prompt": "build A",
                "depends_on": [],
                "checks": ["test A"],
            },
            {
                "id": "B",
                "title": "second",
                "prompt": "build B",
                "depends_on": ["A"],
                "checks": ["test B"],
            },
        ],
        "stage_gate": ["test all"],
    }


def _socket() -> Path:
    return Path("/tmp") / f"swarm-test-{secrets.token_hex(8)}.sock"


@pytest.fixture
def workflow(tmp_path: Path):
    socket = _socket()
    database = tmp_path / "journal.sqlite3"
    server, thread = serve_in_thread(database, socket)
    client = WorkflowClient(JournalClient(socket))
    _bootstrap(client, "run")
    yield client, database, socket
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _bootstrap(client: WorkflowClient, run_id: str, stage: dict | None = None) -> None:
    document = json.dumps(stage or _stage(), separators=(",", ":"))
    client.add(run_id, "launcher", f"bootstrap: run:{run_id}\nstage-json: {document}")


def _open_pr(
    client: WorkflowClient,
    run_id: str,
    worker: str,
    task_id: str,
    base: int,
    head: int,
) -> dict:
    claimed = client.add(run_id, worker, f"claim: task:{task_id}")["work"]
    client.add(run_id, worker, f"checkpoint: task:{task_id}\nimplementation saved")
    client.add(run_id, worker, f"handoff: task:{task_id}\nchecks passed")
    return client.add(
        run_id,
        worker,
        "\n".join(
            (
                f"open-pr: task:{task_id}",
                f"branch: {claimed['branch']}",
                f"base: {base:040x}",
                f"head: {head:040x}",
                "verified: true",
            )
        ),
    )


def _approve(
    client: WorkflowClient,
    run_id: str,
    worker: str,
    task_id: str,
    generation: int,
    ordinal: int,
    head: int,
) -> dict:
    client.add(run_id, worker, f"claim: review:{task_id}:{generation}:{ordinal}")
    return client.add(
        run_id,
        worker,
        "\n".join(
            (
                f"approve: review:{task_id}:{generation}:{ordinal}",
                f"head: {head:040x}",
                "verified: true",
                "evidence: objective and checks passed",
            )
        ),
    )


def _merge(
    client: WorkflowClient,
    run_id: str,
    worker: str,
    task_id: str,
    generation: int,
    head: int,
    merge: int,
) -> None:
    client.add(run_id, worker, f"claim: merge:{task_id}:{generation}")
    client.add(
        run_id,
        worker,
        f"merge: task:{task_id}\ngeneration: {generation}\nhead: {head:040x}",
    )
    client.add(
        run_id,
        "launcher",
        "\n".join(
            (
                "control: merged",
                f"task: {task_id}",
                f"generation: {generation}",
                f"head: {head:040x}",
                f"merge: {merge:040x}",
                f"tree: {merge + 1:040x}",
            )
        ),
    )


def test_kernel_is_only_generic_ordered_append_and_search(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "kernel.sqlite3", clock=lambda: 7.0)
    first = journal.add("space", "alice", "anything: alpha")
    second = journal.add("space", "bob", "anything: beta")
    assert first["record_id"] < second["record_id"]
    assert [item["text"] for item in journal.search("space", "*")] == [
        "anything: alpha",
        "anything: beta",
    ]
    assert [item["author"] for item in journal.search("space", "beta")] == ["bob"]
    with pytest.raises(JournalError, match="only journal_add and journal_search"):
        journal.dispatch("claim", {})


def test_first_valid_claim_wins_by_generic_record_order(workflow) -> None:
    client, _, _ = workflow
    barrier = threading.Barrier(2)

    def claim(worker: str) -> str:
        contender = WorkflowClient(JournalClient(client.socket_path))
        barrier.wait()
        try:
            contender.add("run", worker, "claim: task:A")
            return "accepted"
        except WorkflowError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ("worker-0", "worker-1")))
    assert sorted(outcomes) == ["accepted", "rejected"]
    work = client.search("run", "queue:all")
    assert [(item["task_id"], item["state"]) for item in work] == [
        ("A", "working"),
        ("B", "blocked"),
    ]


def test_pr_requires_three_distinct_internal_reviews_before_merge(workflow) -> None:
    client, database, socket = workflow
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    ready = client.search("run", "queue:ready")
    assert [item["review_role"] for item in ready] == [
        "specification",
        "adversarial",
        "integration",
    ]
    assert client.search("run", "queue:ready", actor="worker-0") == []
    assert len(client.search("run", "queue:ready", actor="worker-1")) == 3
    with pytest.raises(WorkflowError, match="own candidate"):
        client.add("run", "worker-0", "claim: review:A:1:1")
    client.add("run", "worker-1", "claim: review:A:1:1")
    with pytest.raises(WorkflowError, match="owned current candidate"):
        client.add(
            "run",
            "worker-1",
            f"approve: review:A:1:1\nhead: {2:040x}\nverified: true",
        )
    _approve(client, "run", "worker-1", "A", 1, 1, 2)
    with pytest.raises(WorkflowError, match="distinct"):
        client.add("run", "worker-1", "claim: review:A:1:2")
    _approve(client, "run", "worker-2", "A", 1, 2, 2)
    assert all(item["role"].startswith("review:") for item in client.search("run", "queue:ready"))
    _approve(client, "run", "worker-3", "A", 1, 3, 2)
    merge = client.search("run", "queue:ready")
    assert [(item["root_task_id"], item["role"]) for item in merge] == [("A", "merge")]
    _merge(client, "run", "worker-4", "A", 1, 2, 3)
    assert [item["root_task_id"] for item in client.search("run", "queue:ready")] == ["B"]

    restarted = WorkflowClient(JournalClient(socket))
    assert restarted.search("run", "task:A")[0]["merged_sha"] == f"{3:040x}"
    assert Journal(database).search("run", "*")


def test_challenge_invalidates_generation_and_requires_all_reviews_again(workflow) -> None:
    client, _, _ = workflow
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    _approve(client, "run", "worker-1", "A", 1, 1, 2)
    client.add("run", "worker-2", "claim: review:A:1:2")
    client.add(
        "run",
        "worker-2",
        "challenge: review:A:1:2\nhead: " + f"{2:040x}" + "\nverified: true\nreason: defect",
    )
    revision = client.search("run", "queue:ready")[0]
    assert revision["role"] == "revision"
    _open_pr(client, "run", "worker-4", "A", 3, 4)
    summary = client.search("run", "task:A")[0]
    assert summary["generation"] == 2
    assert summary["approvals"] == 0
    assert len(client.search("run", "queue:ready")) == 3
    with pytest.raises(WorkflowError, match="current candidate"):
        client.add(
            "run",
            "worker-3",
            f"approve: review:A:1:3\nhead: {2:040x}\nverified: true",
        )


def test_pause_resume_is_derived_from_generic_records(workflow) -> None:
    client, _, _ = workflow
    assert client.add("run", "launcher", "control: pause")["state"] == "paused"
    with pytest.raises(WorkflowError, match="paused"):
        client.add("run", "worker-0", "claim: task:A")
    assert client.add("run", "launcher", "control: resume")["state"] == "running"


def test_stage0_seven_workers_traverse_author_review_and_merge_roles(tmp_path: Path) -> None:
    stage = copy.deepcopy(load_stage(Path(__file__).parents[1] / "stages" / "stage0.yaml"))
    stage["required_reviews"] = 3
    assert len(stage["tasks"]) == 16
    socket = _socket()
    server, thread = serve_in_thread(tmp_path / "stage0.sqlite3", socket)
    client = WorkflowClient(JournalClient(socket))
    _bootstrap(client, "stage0", stage)
    workers = [f"worker-{index}" for index in range(7)]
    initial = client.search("stage0", "queue:ready")[:7]

    def first_claim(item: dict, worker: str) -> str:
        WorkflowClient(JournalClient(socket)).add("stage0", worker, item["claim"])
        return item["root_task_id"]

    with ThreadPoolExecutor(max_workers=7) as pool:
        claimed = list(pool.map(first_claim, initial, workers))
    assert len(set(claimed)) == 7

    counter = 10

    def finish(worker: str, item: dict) -> None:
        nonlocal counter
        role = str(item["role"])
        task_id = str(item["root_task_id"])
        if role in {"author", "revision"}:
            base, head = counter, counter + 1
            counter += 3
            client.add("stage0", worker, f"checkpoint: task:{task_id}\nsaved")
            client.add("stage0", worker, f"handoff: task:{task_id}\nchecked")
            client.add(
                "stage0",
                worker,
                "\n".join(
                    (
                        f"open-pr: task:{task_id}",
                        f"branch: {item['branch']}",
                        f"base: {base:040x}",
                        f"head: {head:040x}",
                        "verified: true",
                    )
                ),
            )
        elif role.startswith("review:"):
            ordinal = int(item["review_ordinal"])
            client.add(
                "stage0",
                worker,
                "\n".join(
                    (
                        f"approve: review:{task_id}:{item['generation']}:{ordinal}",
                        f"head: {item['head_sha']}",
                        "verified: true",
                        "evidence: simulated criterion and check pass",
                    )
                ),
            )
        elif role == "merge":
            client.add(
                "stage0",
                worker,
                f"merge: task:{task_id}\ngeneration: {item['generation']}\nhead: {item['head_sha']}",
            )
            merge = counter
            counter += 2
            client.add(
                "stage0",
                "launcher",
                "\n".join(
                    (
                        "control: merged",
                        f"task: {task_id}",
                        f"generation: {item['generation']}",
                        f"head: {item['head_sha']}",
                        f"merge: {merge:040x}",
                        f"tree: {merge + 1:040x}",
                    )
                ),
            )
        else:
            raise AssertionError(role)

    while client.search("stage0", "run:state")[0]["state"] == "running":
        progressed = False
        for worker in workers:
            active = client.search("stage0", f"worker:{worker}")
            if active:
                finish(worker, active[0])
                progressed = True
        for item in client.search("stage0", "queue:ready"):
            for worker in workers:
                try:
                    claimed_item = client.add("stage0", worker, item["claim"])["work"]
                except WorkflowError:
                    continue
                finish(worker, claimed_item)
                progressed = True
                break
        assert progressed
    assert client.search("stage0", "run:state")[0]["state"] == "publishing"
    assert all(item["state"] == "complete" for item in client.search("stage0", "queue:all"))
    client.add("stage0", "launcher", f"control: published\ncommit: {999:040x}")
    assert client.search("stage0", "run:state")[0]["state"] == "complete"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
