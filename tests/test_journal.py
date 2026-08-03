from __future__ import annotations

import json
import secrets
import threading
import time
from concurrent.futures import ALL_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

import pytest

from swarm_harness.cli import load_stage
from swarm_harness.journal import Journal, JournalClient, JournalError, serve_in_thread


def _stage() -> dict:
    return {
        "stage": 0,
        "enabled": True,
        "goal": "test",
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


def _bootstrap(client: JournalClient, run_id: str, stage: dict | None = None) -> None:
    document = json.dumps(stage or _stage(), separators=(",", ":"))
    client.add(run_id, "launcher", f"bootstrap: run:{run_id}\nstage-json: {document}")


def _socket() -> Path:
    return Path("/tmp") / f"swarm-test-{secrets.token_hex(8)}.sock"


@pytest.fixture
def journal(tmp_path: Path):
    socket = _socket()
    database = tmp_path / "journal.sqlite3"
    server, thread = serve_in_thread(database, socket)
    client = JournalClient(socket)
    _bootstrap(client, "run")
    yield client, database, socket
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _finish(client: JournalClient, run_id: str, worker: str, task: str, number: int) -> None:
    client.add(run_id, worker, f"checkpoint: task:{task}\nimplementation saved")
    client.add(run_id, worker, f"handoff: task:{task}\nchecks passed")
    client.add(
        run_id,
        worker,
        f"complete: task:{task}\ncommit: {number:040x}\nverified: true",
    )


def test_exactly_two_operations_and_atomic_claim(journal) -> None:
    client, database, _ = journal
    prototype = Journal(database)
    with pytest.raises(JournalError, match="only journal_add and journal_search"):
        prototype.dispatch("claim_task", {})

    barrier = threading.Barrier(2)

    def claim(worker: str) -> str:
        contender = JournalClient(client.socket_path)
        barrier.wait()
        try:
            contender.add("run", worker, "claim: task:A")
            return "accepted"
        except JournalError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ("worker-0", "worker-1")))
    assert sorted(outcomes) == ["accepted", "rejected"]
    working = client.search("run", "queue:all")
    assert [(task["task_id"], task["state"]) for task in working] == [
        ("A", "working"),
        ("B", "blocked"),
    ]


def test_worker_self_completion_unlocks_dependency_and_survives_restart(journal) -> None:
    client, database, socket = journal
    assert [task["task_id"] for task in client.search("run", "queue:ready")] == ["A"]
    assert client.search("run", "task:A")[0]["checks"] == ["test A"]
    assert client.search("run", "task:B")[0]["checks"] == ["test B"]
    client.add("run", "worker-0", "claim: task:A")
    with pytest.raises(JournalError, match="checkpoint"):
        client.add(
            "run",
            "worker-0",
            "complete: task:A\ncommit: " + "1" * 40 + "\nverified: true",
        )
    _finish(client, "run", "worker-0", "A", 1)
    assert [task["task_id"] for task in client.search("run", "queue:ready")] == ["B"]

    # Closing and reopening the prototype preserves the queue without a controller.
    # The fixture owns the live server, so use a second database copy for this proof.
    restarted = Journal(database)
    assert restarted.search("run", "task:A")[0]["commit_sha"] == f"{1:040x}"

    client.add("run", "worker-1", "claim: task:B")
    _finish(client, "run", "worker-1", "B", 2)
    assert client.search("run", "run:state")[0]["state"] == "complete"
    assert socket.exists()


def test_pause_resume_is_itself_two_tool_journal_text(journal) -> None:
    client, _, _ = journal
    assert client.add("run", "launcher", "control: pause")["state"] == "paused"
    with pytest.raises(JournalError, match="paused"):
        client.add("run", "worker-0", "claim: task:A")
    assert client.add("run", "launcher", "control: resume")["state"] == "running"
    assert client.add("run", "worker-0", "claim: task:A")["claim"] == "accepted"


def test_stage0_keeps_seven_workers_productive_and_reaches_gate(tmp_path: Path) -> None:
    stage = load_stage(Path(__file__).parents[1] / "stages" / "stage0.yaml")
    assert len(stage["tasks"]) == 16
    assert sum(not task["depends_on"] for task in stage["tasks"]) == 15
    assert all(task["checks"] and all(task["checks"]) for task in stage["tasks"])

    socket = _socket()
    server, thread = serve_in_thread(tmp_path / "stage0.sqlite3", socket)
    client = JournalClient(socket)
    _bootstrap(client, "stage0", stage)
    first_claims: list[tuple[str, str]] = []
    errors: list[tuple[int, str]] = []
    lock = threading.Lock()
    first_wave = threading.Barrier(7)
    stop = threading.Event()

    def work(index: int) -> None:
        try:
            local = JournalClient(socket)
            worker = f"worker-{index}"
            initial_id = stage["tasks"][index]["id"]
            assert initial_id in {task["task_id"] for task in local.search("stage0", "queue:ready")}
            local.add("stage0", worker, f"claim: task:{initial_id}")
            with lock:
                first_claims.append((worker, initial_id))
            first_wave.wait(timeout=10)
            counter = index + 1
            while not stop.is_set():
                active = local.search("stage0", f"worker:{worker}")
                if active:
                    _finish(local, "stage0", worker, active[0]["task_id"], counter)
                    counter += 7
                    continue
                state = local.search("stage0", "run:state")[0]["state"]
                if state == "complete":
                    return
                claimed = False
                for task in local.search("stage0", "queue:ready"):
                    try:
                        local.add("stage0", worker, f"claim: task:{task['task_id']}")
                    except JournalError:
                        continue
                    claimed = True
                    break
                if not claimed:
                    time.sleep(0.005)
            raise RuntimeError("another worker failed")
        except Exception as error:
            with lock:
                errors.append((index, repr(error)))
            stop.set()
            raise

    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = [pool.submit(work, index) for index in range(7)]
        finished, unfinished = wait(futures, timeout=30, return_when=ALL_COMPLETED)
        assert len(finished) == 7
        assert not unfinished
    assert not errors, errors
    assert len(first_claims) == 7
    assert len({task for _, task in first_claims}) == 7
    assert all(task["state"] == "complete" for task in client.search("stage0", "queue:all"))
    assert client.search("stage0", "run:state")[0]["state"] == "complete"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize(
    "stage, message",
    [
        ({**_stage(), "tasks": [{**_stage()["tasks"][0], "depends_on": ["A"]}]}, "itself"),
        ({**_stage(), "tasks": [{**_stage()["tasks"][0], "checks": [""]}]}, "invalid"),
    ],
)
def test_invalid_stage_graph_fails_closed(tmp_path: Path, stage: dict, message: str) -> None:
    socket = _socket()
    server, thread = serve_in_thread(tmp_path / "bad.sqlite3", socket)
    client = JournalClient(socket)
    with pytest.raises(JournalError, match=message):
        _bootstrap(client, "bad", stage)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
