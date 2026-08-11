from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from oxide.journal import Journal, JournalClient, JournalError, serve_in_thread
from oxide.workflow import WorkflowClient, WorkflowError, WorkflowReducer


def _stage() -> dict:
    return {
        "stage": "test",
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
    }


def _socket() -> Path:
    return Path("/tmp") / f"oxide-test-{secrets.token_hex(8)}.sock"


def _client(socket: Path | str) -> WorkflowClient:
    return WorkflowClient(JournalClient(socket), _stage())


def _exact_count(database: Path, namespace: str, query: str) -> int:
    return sum(item["match_kind"] == "exact" for item in Journal(database).search(namespace, query))


@pytest.fixture
def workflow(tmp_path: Path):
    socket = _socket()
    database = tmp_path / "journal.sqlite3"
    server, thread = serve_in_thread(database, socket)
    client = _client(socket)
    _bootstrap(client, "run")
    yield client, database, socket
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def _bootstrap(client: WorkflowClient, run_id: str, stage: dict | None = None) -> None:
    if stage is not None:
        client._workload = stage
        client.workload_ref = {
            "schema": "OxideVerificationContractRefV1",
            "contract_blob": hashlib.sha256(
                json.dumps(stage, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        client.reducer = type(client.reducer)(stage, client.workload_ref)
    client.bootstrap(run_id)


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
    proposed = client.add(
        run_id,
        worker,
        "\n".join(
            (
                f"open-pr: task:{task_id}",
                f"branch: {claimed['branch']}",
                f"base: {base:040x}",
                f"head: {head:040x}",
                f"tree: {head:040x}",
                "verified: true",
            )
        ),
    )
    return client.add(
        run_id,
        "launcher",
        "\n".join(
            (
                "control: candidate-qualified",
                f"task: {task_id}",
                f"generation: {proposed['generation']}",
                f"head: {head:040x}",
                f"tree: {head:040x}",
                f"receipt: sha256:{'a' * 64}",
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


def _verify(
    client: WorkflowClient,
    run_id: str,
    worker: str,
    task_id: str,
    head: int,
    ordinal: int = 1,
    *,
    result: str = "passed",
    preclaimed: bool = False,
) -> dict:
    identity = f"verify:{task_id}:{head:040x}:{ordinal}"
    if preclaimed:
        work = next(
            item
            for item in client.search(run_id, f"worker:{worker}")
            if item.get("claim") == f"claim: {identity}"
        )
    else:
        work = client.add(run_id, worker, f"claim: {identity}")["work"]
    marker = {
        "passed": "verify-pass",
        "product_failure": "verify-fail",
        "infrastructure_failure": "verify-infrastructure",
    }[result]
    detail = "evidence" if result == "passed" else "reason"
    receipt = (
        "sha256:"
        + hashlib.sha256(f"{identity}:{work['execution_attempt']}:{result}".encode()).hexdigest()
    )
    return client.add(
        run_id,
        worker,
        "\n".join(
            (
                f"{marker}: {identity}",
                f"head: {head:040x}",
                f"tree: {work['tree_sha']}",
                f"base: {work['evidence_requirement']['candidate']['base']}",
                f"evidence-key: {work['evidence_key']}",
                f"claim-attempt: {work['execution_attempt']}",
                f"execution-attempt: {work['execution_attempt']}",
                f"receipt: {receipt}",
                "verified: true",
                f"{detail}: exact frozen command {result}",
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
                f"candidate-tree: {head:040x}",
                "prospective-receipt:",
            )
        ),
    )


def _race_claim(socket: str, claim_text: str, workers: tuple[str, str]) -> dict[str, str]:
    barrier = threading.Barrier(len(workers))

    def claim(worker: str) -> tuple[str, str]:
        contender = _client(socket)
        barrier.wait()
        try:
            contender.add("run", worker, claim_text)
            return worker, "accepted"
        except WorkflowError:
            return worker, "rejected"

    with ThreadPoolExecutor(max_workers=len(workers)) as pool:
        return dict(pool.map(claim, workers))


def test_kernel_is_only_generic_ordered_append_and_search(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "kernel.sqlite3", clock=lambda: 7.0)
    first = journal.add("space", "alice", "anything: alpha")
    second = journal.add("space", "bob", "anything: beta")
    assert first["record_id"] < second["record_id"]
    assert journal.search("space", "*") == []
    assert [item["text"] for item in journal.search("space", "anything")] == [
        "anything: alpha",
        "anything: beta",
    ]
    assert [item["author"] for item in journal.search("space", "beta")] == ["bob"]
    with pytest.raises(JournalError, match="only journal_add and journal_search"):
        journal.dispatch("claim", {})


def test_warm_search_observes_records_appended_by_another_client(tmp_path: Path) -> None:
    database = tmp_path / "kernel.sqlite3"
    journal = Journal(database)
    journal.add("space", "alice", "anything: alpha")
    assert [item["text"] for item in journal.search("space", "anything")] == ["anything: alpha"]

    Journal(database).add("space", "bob", "anything: beta")

    assert [item["text"] for item in journal.search("space", "anything")] == [
        "anything: alpha",
        "anything: beta",
    ]


def test_prototype_builds_and_repairs_rebuildable_search_indexes(tmp_path: Path) -> None:
    database = tmp_path / "kernel.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE records (
              record_id INTEGER PRIMARY KEY AUTOINCREMENT,
              namespace TEXT NOT NULL,
              author TEXT NOT NULL,
              text TEXT NOT NULL,
              created_at REAL NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO records(namespace,author,text,created_at) VALUES(?,?,?,?)",
            (
                (
                    "space",
                    "alice",
                    "prefix literal-routing:alpha:000000000001 suffix",
                    1.0,
                ),
                ("space", "bob", "related routing beta", 2.0),
            ),
        )

    migrated = Journal(database)
    expected_exact = migrated.search("space", "literal-routing:alpha:000000000001")
    expected_semantic = migrated.search("space", "routing beta")
    with sqlite3.connect(database) as connection:
        index_namespace_id, gram = connection.execute(
            "SELECT index_namespace_id,gram FROM exact_postings LIMIT 1"
        ).fetchone()
        plan = connection.execute(
            """
            EXPLAIN QUERY PLAN
            SELECT records.record_id
            FROM exact_postings JOIN records USING(record_id)
            WHERE exact_postings.index_namespace_id=?
              AND exact_postings.gram=?
              AND records.namespace=?
            """,
            (index_namespace_id, gram, "space"),
        ).fetchall()
        connection.execute("DELETE FROM exact_postings")

    exact_repaired = Journal(database)
    assert any("exact_postings" in str(row[-1]) for row in plan)
    assert exact_repaired.search("space", "literal-routing:alpha:000000000001") == (expected_exact)
    assert exact_repaired.search("space", "routing beta") == expected_semantic

    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM lexical_postings")
    lexical_repaired = Journal(database)
    assert lexical_repaired.search("space", "literal-routing:alpha:000000000001") == (
        expected_exact
    )
    assert lexical_repaired.search("space", "routing beta") == expected_semantic


def test_record_acknowledgement_and_derived_index_publication_are_atomic(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "kernel.sqlite3"
    journal = Journal(database)
    index_record = journal._index_record

    def fail_index(*_args, **_kwargs) -> None:
        raise sqlite3.OperationalError("injected derived-index failure")

    monkeypatch.setattr(journal, "_index_record", fail_index)
    with pytest.raises(JournalError, match="index publication failed"):
        journal.add("space", "alice", "atomic literal-routing:000000000001")

    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM exact_postings").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM lexical_postings").fetchone()[0] == 0

    monkeypatch.setattr(journal, "_index_record", index_record)
    added = journal.add("space", "alice", "atomic literal-routing:000000000001")
    found = journal.search("space", "literal-routing:000000000001")
    assert [item["record_id"] for item in found] == [added["record_id"]]
    assert found[0]["match_kind"] == "exact"


def test_sparse_exact_index_covers_every_byte_alignment(tmp_path: Path) -> None:
    journal = Journal(tmp_path / "kernel.sqlite3", min_exact=5, max_results=64)
    query = "literal-byte-sequence:0123456789abcdef"
    for offset in range(32):
        journal.add("space", "worker", f"{'x' * offset}{query}:suffix-{offset}")
    journal.add("space", "worker", "literal-byte-sequence differs")

    found = journal.search("space", query)
    assert [item["record_id"] for item in found] == list(range(1, 33))
    assert all(item["match_kind"] == "exact" for item in found)


def test_lexical_candidate_cover_preserves_every_threshold_eligible_record(
    tmp_path: Path,
) -> None:
    journal = Journal(tmp_path / "kernel.sqlite3", min_exact=5, max_results=64)
    terms = ("alpha", "beta", "gamma", "delta", "epsilon")
    eligible_ids = []
    for mask in range(1, 1 << len(terms)):
        selected = [term for index, term in enumerate(terms) if mask & (1 << index)]
        added = journal.add("space", "worker", " ".join((*selected, f"record-{mask}")))
        if len(selected) >= 3:
            eligible_ids.append(added["record_id"])

    found = journal.search("space", " ".join(terms))
    assert [item["record_id"] for item in found] == eligible_ids


def test_derived_lexical_candidate_cannot_fabricate_semantic_authority(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kernel.sqlite3"
    journal = Journal(database)
    added = journal.add("space", "worker", "unrelated immutable source")
    with sqlite3.connect(database) as connection:
        index_namespace_id = connection.execute(
            "SELECT index_namespace_id FROM index_namespaces WHERE namespace='space'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO lexical_postings(index_namespace_id,term,record_id) VALUES(?,?,?)",
            (index_namespace_id, "fabricated", added["record_id"]),
        )
        connection.execute(
            "INSERT INTO lexical_counts(index_namespace_id,term,record_count) VALUES(?,?,1)",
            (index_namespace_id, "fabricated"),
        )

    assert journal.search("space", "fabricated") == []


def test_derived_exact_fingerprint_cannot_fabricate_literal_authority(
    tmp_path: Path,
) -> None:
    database = tmp_path / "kernel.sqlite3"
    journal = Journal(database)
    added = journal.add("space", "worker", "unrelated immutable source material")
    query = "fabricated-exact-byte-sequence-0123456789abcdef"
    gram = journal._exact_fingerprint(query.encode()[:16])
    with sqlite3.connect(database) as connection:
        index_namespace_id = connection.execute(
            "SELECT index_namespace_id FROM index_namespaces WHERE namespace='space'"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO exact_postings(index_namespace_id,gram,record_id) VALUES(?,?,?)",
            (index_namespace_id, gram, added["record_id"]),
        )
        connection.execute(
            "INSERT INTO exact_counts(index_namespace_id,gram,record_count) VALUES(?,?,1)",
            (index_namespace_id, gram),
        )

    assert journal.search("space", query) == []


def test_exact_and_lexical_indexes_scale_better_than_authority_scan(tmp_path: Path) -> None:
    database = tmp_path / "scaled.sqlite3"

    def route(ordinal: int) -> str:
        digest = hashlib.sha256(str(ordinal).encode()).hexdigest()
        return f"routing:{digest}:{ordinal:064b}"

    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE records (
              record_id INTEGER PRIMARY KEY AUTOINCREMENT,
              namespace TEXT NOT NULL,
              author TEXT NOT NULL,
              text TEXT NOT NULL,
              created_at REAL NOT NULL
            )
            """
        )
        rows = []
        for namespace, size in (("small", 300), ("large", 3_000)):
            rows.extend(
                (
                    namespace,
                    "worker",
                    " ".join(
                        (
                            route(ordinal),
                            "shared-token",
                            f"component-specific-{ordinal:06d}",
                            "alpha beta",
                        )
                    ),
                    float(ordinal),
                )
                for ordinal in range(size)
            )
        connection.executemany(
            "INSERT INTO records(namespace,author,text,created_at) VALUES(?,?,?,?)",
            rows,
        )

    journal = Journal(database)
    exact_query = route(0)
    lexical_query = "component-specific-000000 shared-token absent-token"
    token = re.compile(r"[\w-]+", re.UNICODE)

    def authority_scan(namespace: str, query: str) -> list[int]:
        query_terms = {item.casefold() for item in token.findall(query)}
        with sqlite3.connect(database) as connection:
            records = connection.execute(
                "SELECT record_id,text FROM records WHERE namespace=? ORDER BY record_id",
                (namespace,),
            ).fetchall()
        qualifying = []
        for record_id, text in records:
            terms = {item.casefold() for item in token.findall(text)}
            if query in text or len(query_terms & terms) / len(query_terms) >= 0.6:
                qualifying.append(int(record_id))
        return qualifying[-10:]

    for namespace in ("small", "large"):
        assert [
            item["record_id"] for item in journal.search(namespace, exact_query)
        ] == authority_scan(namespace, exact_query)
        assert [
            item["record_id"] for item in journal.search(namespace, lexical_query)
        ] == authority_scan(namespace, lexical_query)

    def elapsed(operation) -> float:
        operation()
        samples = []
        for _ in range(3):
            started = time.perf_counter()
            for _ in range(20):
                operation()
            samples.append(time.perf_counter() - started)
        return min(samples)

    small_exact = elapsed(lambda: journal.search("small", exact_query))
    large_exact = elapsed(lambda: journal.search("large", exact_query))
    large_exact_scan = elapsed(lambda: authority_scan("large", exact_query))
    small_lexical = elapsed(lambda: journal.search("small", lexical_query))
    large_lexical = elapsed(lambda: journal.search("large", lexical_query))
    large_lexical_scan = elapsed(lambda: authority_scan("large", lexical_query))

    assert large_exact < large_exact_scan / 2
    assert large_lexical < large_lexical_scan / 2
    assert large_exact < small_exact * 3
    assert large_lexical < small_lexical * 3


def test_bootstrap_cites_frozen_workload_without_journalizing_specification(
    workflow,
) -> None:
    client, _, _ = workflow
    bootstrap = client.replay_records("run")[0]["text"]
    assert "workload-ref:" in bootstrap
    assert "build A" not in bootstrap
    assert "test all" not in bootstrap
    assert "stage-json" not in bootstrap


def test_epoch_frontiers_never_rehabilitate_a_previously_stale_record() -> None:
    stage = _stage()
    reference = {"schema": "OxideVerificationContractRefV1", "contract_blob": "fixture"}
    replay_root = "a" * 32

    def record(sequence: int, epoch: int, body: str) -> dict:
        return {
            "record_id": sequence,
            "stable_id": f"record:{sequence}",
            "journal_sequence": sequence,
            "namespace": "run",
            "author": "launcher" if sequence == 1 else "worker-0",
            "text": "\n".join(
                (
                    body,
                    "oxide-run:run",
                    f"oxide-epoch:{epoch}",
                    f"oxide-stable:{sequence:032x}",
                    f"oxide-routing:{replay_root}:{sequence:064b}",
                )
            ),
            "created_at": float(sequence),
            "match_kind": "exact",
        }

    records = [
        record(1, 0, f"bootstrap: run:run\nworkload-ref: {json.dumps(reference)}"),
        record(2, 1, "discovery: valid epoch-one evidence"),
        record(3, 0, "claim: task:A"),
    ]
    view = WorkflowReducer(
        stage,
        reference,
        epoch=2,
        history_sequence=3,
        epoch_frontiers=[{"epoch": 0, "through": 1}, {"epoch": 1, "through": 3}],
    ).reduce("run", records)
    assert view.outcomes[1][0] is True
    assert view.outcomes[2][0] is True
    assert view.outcomes[3] == (False, "workflow record carries a stale run epoch")
    assert view.tasks["A"]["state"] == "pending"


def test_workflow_replay_recovers_every_record_beyond_search_result_caps() -> None:
    replay_root = "a" * 32
    records = [
        {
            "record_id": index + 1,
            "journal_sequence": index + 1,
            "stable_id": f"record:{index + 1}",
            "match_kind": "exact",
            "namespace": "product",
            "author": "worker-0",
            "text": "\n".join(
                (
                    f"audit: {index}",
                    "oxide-run:product",
                    "oxide-epoch:0",
                    f"oxide-stable:{index:032x}",
                    f"oxide-routing:{replay_root}:{index:064b}",
                )
            ),
            "created_at": float(index),
        }
        for index in range(1_500)
    ]
    semantic_noise = {
        "record_id": 99_999,
        "journal_sequence": 99_999,
        "stable_id": "record:99999",
        "match_kind": "semantic",
        "namespace": "product",
        "author": "memory",
        "text": "\n".join(
            (
                "related replay routing concept",
                "oxide-run:product",
                "oxide-epoch:0",
                f"oxide-stable:{9_999:032x}",
                f"oxide-routing:{'b' * 32}:{9_999:064b}",
            )
        ),
        "created_at": 99_999.0,
    }

    class CappedPort:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def add(self, _namespace: str, _author: str, _text: str) -> dict:
            raise AssertionError("replay is read-only")

        def search(self, namespace: str, query: str) -> list[dict]:
            assert namespace == "product"
            assert query != "*"
            self.queries.append(query)
            exact = [record for record in records if query in record["text"]][-1:]
            return [*exact, semantic_noise]

    backend = CappedPort()
    assert (
        len(
            WorkflowClient(backend, replay_root=replay_root).replay_records("product")  # type: ignore[arg-type]
        )
        == 1_500
    )
    assert len(backend.queries) == len(records) + 1


def test_semantic_noise_does_not_make_an_empty_replay_partition_nonempty() -> None:
    replay_root = "a" * 32
    noise = {
        "record_id": 1,
        "journal_sequence": 1,
        "stable_id": "record:1",
        "match_kind": "semantic",
        "namespace": "run",
        "author": "memory",
        "text": "\n".join(
            (
                "semantically related routing memory",
                "oxide-run:run",
                "oxide-epoch:0",
                f"oxide-stable:{1:032x}",
                f"oxide-routing:{'b' * 32}:{1:064b}",
            )
        ),
        "created_at": 1.0,
    }

    class SemanticOnlyPort:
        def add(self, _namespace: str, _author: str, _text: str) -> dict:
            raise AssertionError("replay is read-only")

        def search(self, namespace: str, query: str) -> list[dict]:
            assert namespace == "run"
            assert query.startswith(f"{replay_root}:")
            return [noise]

    client = WorkflowClient(SemanticOnlyPort(), replay_root=replay_root)  # type: ignore[arg-type]
    exact, returned = client._partition_records("run", "")
    assert exact == [] and returned == [noise]
    assert client.replay_records("run") == []


def test_cached_projection_recovers_only_the_new_replay_leaves(workflow, monkeypatch) -> None:
    writer, _, socket = workflow
    observer = _client(socket)
    assert observer.search("run", "run:state")[0]["state"] == "running"

    writer.add("run", "worker-0", "claim: task:A")
    writer.add("run", "worker-0", "checkpoint: task:A\nimplementation saved")
    monkeypatch.setattr(
        observer,
        "_records",
        lambda _namespace: (_ for _ in ()).throw(AssertionError("full replay")),
    )

    task = next(item for item in observer.search("run", "queue:all") if item["task_id"] == "A")
    assert task["state"] == "working"
    assert task["checkpoint"] is True
    assert len(observer._views["run"].records) == 3


def test_first_valid_claim_wins_by_generic_record_order(workflow) -> None:
    client, _, socket = workflow
    outcomes = _race_claim(socket, "claim: task:A", ("worker-0", "worker-1"))
    assert sorted(outcomes.values()) == ["accepted", "rejected"]
    winner = next(worker for worker, outcome in outcomes.items() if outcome == "accepted")
    checkpoint = client.add("run", winner, "checkpoint: task:A\nimplementation saved")
    work = client.search("run", "queue:all")
    assert [(item["task_id"], item["state"]) for item in work] == [
        ("A", "working"),
        ("B", "blocked"),
    ]
    active = work[0]
    assert active["checkpoint"] is True
    assert active["handoff"] is False
    assert active["last_journal_record_id"] == checkpoint["record_id"]
    assert active["last_journal_body"] == "checkpoint: task:A\nimplementation saved"


def test_launcher_reclaims_crashed_author_review_and_merge_owners(workflow) -> None:
    client, _, _ = workflow
    client.add("run", "worker-0", "claim: task:A")
    client.add("run", "worker-0", "checkpoint: task:A\nsaved")
    with pytest.raises(WorkflowError, match="only the launcher"):
        client.add("run", "worker-1", "control: reclaim worker:worker-0")
    assert client.add("run", "launcher", "control: reclaim worker:worker-0") == {
        "saved": True,
        "reclaimed": "A",
        "record_id": 5,
    }
    ready = client.search("run", "queue:ready")[0]
    assert ready["root_task_id"] == "A" and not ready["checkpoint"]
    _open_pr(client, "run", "worker-1", "A", 1, 2)

    client.add("run", "worker-2", "claim: review:A:1:1")
    assert client.add("run", "launcher", "control: reclaim worker:worker-2")["reclaimed"] == (
        "A/review-1"
    )
    _approve(client, "run", "worker-3", "A", 1, 1, 2)
    _approve(client, "run", "worker-4", "A", 1, 2, 2)
    _approve(client, "run", "worker-5", "A", 1, 3, 2)
    _verify(client, "run", "worker-6", "A", 2)

    client.add("run", "worker-6", "claim: merge:A:1")
    assert client.add("run", "launcher", "control: reclaim worker:worker-6")["reclaimed"] == (
        "A/merge"
    )
    assert client.add("run", "worker-0", "claim: merge:A:1")["claim"] == "accepted"


def test_internal_verification_review_claim_uses_the_same_atomic_race(workflow) -> None:
    client, database, socket = workflow
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    claim = "claim: review:A:1:1"
    assert client.search("run", "queue:ready")[0]["claim"] == claim
    assert _client(socket).search("run", "queue:ready")[0]["claim"] == claim

    outcomes = _race_claim(socket, claim, ("worker-1", "worker-2"))
    assert sorted(outcomes.values()) == ["accepted", "rejected"]
    winner = next(worker for worker, outcome in outcomes.items() if outcome == "accepted")
    loser = next(worker for worker, outcome in outcomes.items() if outcome == "rejected")
    review = client.search("run", "task:A")[0]["reviews"][0]
    assert (review["state"], review["worker_id"]) == ("claimed", winner)
    assert all(item["claim"] != claim for item in _client(socket).search("run", "queue:ready"))
    assert loser not in {item["worker_id"] for item in client.search("run", "queue:all")}
    active_review = next(
        item
        for item in client.search("run", "queue:all")
        if item["claim"] == claim and item["state"] == "working"
    )
    assert active_review["last_journal_body"] == claim
    assert _exact_count(database, "run", claim) == 2


def test_idle_workers_atomically_claim_candidate_frontier_verification(workflow) -> None:
    client, database, socket = workflow
    client.add("run", "launcher", "control: worker-capacity\nworkers: 8")
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    candidate_claim = f"claim: verify:A:{2:040x}:1"
    candidate = next(
        item for item in client.search("run", "queue:ready") if item["claim"] == candidate_claim
    )
    assert candidate["role"] == "verification"
    _approve(client, "run", "worker-1", "A", 1, 1, 2)
    _approve(client, "run", "worker-2", "A", 1, 2, 2)
    _approve(client, "run", "worker-3", "A", 1, 3, 2)
    outcomes = _race_claim(socket, candidate_claim, ("worker-5", "worker-6"))
    assert sorted(outcomes.values()) == ["accepted", "rejected"]
    winner = next(worker for worker, outcome in outcomes.items() if outcome == "accepted")
    active = client.search("run", f"worker:{winner}")
    assert [(item["role"], item["claim"]) for item in active] == [("verification", candidate_claim)]
    work = active[0]
    with pytest.raises(WorkflowError, match="owned exact frontier"):
        client.add(
            "run",
            winner,
            "\n".join(
                (
                    f"verify-pass: verify:A:{2:040x}:1",
                    f"head: {2:040x}",
                    f"tree: {work['tree_sha']}",
                    f"base: {work['evidence_requirement']['candidate']['base']}",
                    f"evidence-key: {work['evidence_key']}",
                    f"claim-attempt: {work['execution_attempt']}",
                    f"execution-attempt: {'f' * 64}",
                    f"receipt: sha256:{'e' * 64}",
                    "verified: true",
                    "evidence: exact command passed",
                )
            ),
        )
    with pytest.raises(WorkflowError, match="exact frontier"):
        client.add(
            "run",
            winner,
            f"verify-pass: verify:A:{2:040x}:1\nhead: {4:040x}\nverified: true\nevidence: pass",
        )
    result = _verify(client, "run", winner, "A", 2, preclaimed=True)
    assert result["verification"] == "passed"
    assert all(item["claim"] != candidate_claim for item in client.search("run", "queue:ready"))
    assert client.search("run", "task:A")[0]["verifications"][0]["state"] == "passed"
    assert _exact_count(database, "run", candidate_claim) == 2
    _merge(client, "run", "worker-4", "A", 1, 2, 3)
    assert ("B", "author") in {
        (item["root_task_id"], item["role"]) for item in client.search("run", "queue:ready")
    }


def test_launcher_reclaims_crashed_frontier_verifier(workflow) -> None:
    client, _, _ = workflow
    client.add("run", "launcher", "control: worker-capacity\nworkers: 8")
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    claim = f"claim: verify:A:{2:040x}:1"
    client.add("run", "worker-5", claim)
    reclaimed = client.add("run", "launcher", "control: reclaim worker:worker-5")
    assert reclaimed["reclaimed"] == "A/verify-1"
    assert claim in {item["claim"] for item in client.search("run", "queue:ready")}


def test_revision_claim_uses_the_same_atomic_race(workflow) -> None:
    client, database, socket = workflow
    client.add("run", "launcher", "control: worker-capacity\nworkers: 8")
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    verification = f"claim: verify:A:{2:040x}:1"
    client.add("run", "worker-4", verification)
    _verify(client, "run", "worker-4", "A", 2, preclaimed=True)
    client.add("run", "worker-1", "claim: review:A:1:1")
    client.add(
        "run",
        "worker-1",
        f"challenge: review:A:1:1\nhead: {2:040x}\nverified: true\nreason: defect",
    )
    claim = "claim: task:A"
    assert client.search("run", "queue:ready")[0]["claim"] == claim

    outcomes = _race_claim(socket, claim, ("worker-2", "worker-3"))
    assert sorted(outcomes.values()) == ["accepted", "rejected"]
    winner = next(worker for worker, outcome in outcomes.items() if outcome == "accepted")
    work = client.search("run", f"worker:{winner}")[0]
    assert (work["role"], work["claim"]) == ("revision", claim)
    assert verification not in {item["claim"] for item in client.search("run", "queue:ready")}
    loser = next(worker for worker, outcome in outcomes.items() if outcome == "rejected")
    assert _client(socket).search("run", f"worker:{loser}") == []
    assert _exact_count(database, "run", claim) == 3

    _open_pr(client, "run", winner, "A", 3, 4)
    revised = f"claim: verify:A:{4:040x}:1"
    assert revised in {item["claim"] for item in client.search("run", "queue:ready")}
    assert verification not in {item["claim"] for item in client.search("run", "queue:ready")}


def test_terminal_blocker_activation_is_forward_only_and_parks_exact_revision(workflow) -> None:
    client, database, _ = workflow
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    client.add("run", "worker-1", "claim: review:A:1:1")
    client.add(
        "run",
        "worker-1",
        f"challenge: review:A:1:1\nhead: {2:040x}\nverified: true\nreason: defect",
    )
    work = client.add("run", "worker-2", "claim: task:A")["work"]
    blocker = "\n".join(
        (
            "blocked: task:A",
            "role: revision",
            f"branch: {work['branch']}",
            "generation: 1",
            f"head: {2:040x}",
            "verified: false",
            "reason: required external service is unavailable",
        )
    )

    # Historical blocker prose remains inert when the activation appears later.
    assert client.add("run", "worker-2", blocker)["saved"] is True
    client.add("run", "launcher", "control: worker-capacity\nworkers: 8\nterminal-blockers: true")
    assert client.search("run", "task:A")[0]["state"] == "authoring"
    with pytest.raises(WorkflowError, match="must start with exact"):
        client.add("run", "worker-2", blocker.replace("blocked:", "blocker:", 1))

    result = client.add("run", "worker-2", blocker)
    assert result == {
        "saved": True,
        "blocked": "A",
        "state": "blocked",
        "record_id": result["record_id"],
    }
    assert client.search("run", "worker:worker-2") == []
    summary = client.search("run", "task:A")[0]
    assert summary["state"] == "blocked"
    assert summary["last_error"] == "required external service is unavailable"
    assert all(item["root_task_id"] != "A" for item in client.search("run", "queue:ready"))
    with pytest.raises(WorkflowError, match="owned exact author assignment"):
        client.add("run", "worker-2", blocker)

    client.add("run", "launcher", "control: pause")
    retried = client.add("run", "launcher", "control: resume")
    assert retried["state"] == "running"
    assert client.search("run", "task:A")[0]["state"] == "blocked"
    with pytest.raises(WorkflowError, match="paused launcher"):
        client.add("run", "worker-0", "control: retry task:A")
    client.add("run", "launcher", "control: pause")
    retried = client.add("run", "launcher", "control: retry task:A")
    assert retried == {
        "saved": True,
        "retried": "A",
        "state": "revision",
        "record_id": retried["record_id"],
    }
    assert client.search("run", "task:A")[0]["state"] == "revision"
    assert client.add("run", "launcher", "control: resume")["state"] == "running"
    assert client.search("run", "queue:ready")[0]["root_task_id"] == "A"
    assert _exact_count(database, "run", "blocked: task:A") == 3


def test_task_orientation_returns_current_summary_without_history_dump(workflow) -> None:
    client, _, _ = workflow
    client.add("run", "worker-0", "claim: task:A")
    client.add("run", "worker-0", "checkpoint: task:A\nimplementation saved")
    orientation = client.search("run", "task:A")
    assert len(orientation) == 1
    assert orientation[0]["kind"] == "task"
    assert orientation[0]["root_task_id"] == "A"
    targeted = client.search("run", "checkpoint: task:A")
    assert [item["body"] for item in targeted if item["match_kind"] == "exact"] == [
        "checkpoint: task:A\nimplementation saved"
    ]


def test_prior_generation_author_may_review_the_revised_candidate(workflow) -> None:
    client, _, _ = workflow
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    client.add("run", "worker-1", "claim: review:A:1:1")
    client.add(
        "run",
        "worker-1",
        f"challenge: review:A:1:1\nhead: {2:040x}\nverified: true\nreason: defect",
    )
    _open_pr(client, "run", "worker-2", "A", 3, 4)
    claimed = client.add("run", "worker-0", "claim: review:A:2:1")
    assert claimed["claim"] == "accepted"


def test_merge_claim_uses_the_same_atomic_race(workflow) -> None:
    client, database, socket = workflow
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    _approve(client, "run", "worker-1", "A", 1, 1, 2)
    _approve(client, "run", "worker-2", "A", 1, 2, 2)
    _approve(client, "run", "worker-3", "A", 1, 3, 2)
    _verify(client, "run", "worker-6", "A", 2)
    claim = "claim: merge:A:1"
    assert client.search("run", "queue:ready")[0]["claim"] == claim

    outcomes = _race_claim(socket, claim, ("worker-4", "worker-5"))
    assert sorted(outcomes.values()) == ["accepted", "rejected"]
    winner = next(worker for worker, outcome in outcomes.items() if outcome == "accepted")
    work = client.search("run", f"worker:{winner}")[0]
    assert (work["role"], work["claim"]) == ("merge", claim)
    loser = next(worker for worker, outcome in outcomes.items() if outcome == "rejected")
    assert _client(socket).search("run", f"worker:{loser}") == []
    assert _exact_count(database, "run", claim) == 2


def test_pr_requires_three_internal_review_decisions_before_merge(workflow) -> None:
    client, database, socket = workflow
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    ready = client.search("run", "queue:ready")
    assert [item["review_role"] for item in ready if str(item["role"]).startswith("review:")] == [
        "specification",
        "adversarial",
        "integration",
    ]
    client.add("run", "worker-1", "claim: review:A:1:1")
    with pytest.raises(WorkflowError, match="owned current candidate"):
        client.add(
            "run",
            "worker-1",
            f"approve: review:A:1:1\nhead: {2:040x}\nverified: true",
        )
    _approve(client, "run", "worker-1", "A", 1, 1, 2)
    _approve(client, "run", "worker-2", "A", 1, 2, 2)
    assert any(item["role"].startswith("review:") for item in client.search("run", "queue:ready"))
    _approve(client, "run", "worker-3", "A", 1, 3, 2)
    _verify(client, "run", "worker-5", "A", 2)
    merge = client.search("run", "queue:ready")
    assert [(item["root_task_id"], item["role"]) for item in merge] == [("A", "merge")]
    _merge(client, "run", "worker-4", "A", 1, 2, 3)
    assert [item["root_task_id"] for item in client.search("run", "queue:ready")] == ["B"]

    restarted = _client(socket)
    assert restarted.search("run", "task:A")[0]["merged_sha"] == f"{3:040x}"
    assert Journal(database).search("run", "bootstrap: run:run")


def test_worker_verification_and_review_quorum_jointly_authorize_merge(workflow) -> None:
    client, _, socket = workflow
    client.add("run", "launcher", "control: worker-capacity\nworkers: 8")
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    _approve(client, "run", "worker-1", "A", 1, 1, 2)
    _approve(client, "run", "worker-2", "A", 1, 2, 2)
    _approve(client, "run", "worker-3", "A", 1, 3, 2)
    ready = client.search("run", "queue:ready")
    assert [(item["role"], item["claim"]) for item in ready] == [
        ("verification", f"claim: verify:A:{2:040x}:1")
    ]
    client.add("run", "worker-4", f"claim: verify:A:{2:040x}:1")
    _verify(client, "run", "worker-4", "A", 2, preclaimed=True)
    assert client.search("run", "queue:ready")[0]["role"] == "merge"
    restarted = _client(socket)
    assert restarted.search("run", "queue:ready")[0]["role"] == "merge"


def test_published_candidate_exposes_shared_checks_and_reviews_concurrently(
    tmp_path: Path,
) -> None:
    stage = _stage()
    stage["tasks"][0]["checks"] = ["test A one", "test A two"]
    stage["tasks"][1]["depends_on"] = []
    socket = _socket()
    server, thread = serve_in_thread(tmp_path / "shared-checks.sqlite3", socket)
    client = WorkflowClient(JournalClient(socket), stage)
    try:
        _bootstrap(client, "shared", stage)
        client.add("shared", "launcher", "control: worker-capacity\nworkers: 6")

        author = client.add("shared", "worker-0", "claim: task:A")["work"]
        assert author["checks"] == []
        client.add("shared", "worker-0", "checkpoint: task:A\ncandidate committed")
        client.add("shared", "worker-0", "handoff: task:A\ncandidate pushed")
        proposed = client.add(
            "shared",
            "worker-0",
            "\n".join(
                (
                    "open-pr: task:A",
                    f"branch: {author['branch']}",
                    f"base: {1:040x}",
                    f"head: {2:040x}",
                    f"tree: {2:040x}",
                    "verified: true",
                )
            ),
        )

        assert client.search("shared", "task:A")[0]["state"] == "qualifying"
        ready = client.search("shared", "queue:ready")
        assert not any(item["root_task_id"] == "A" for item in ready)
        client.add(
            "shared",
            "launcher",
            "\n".join(
                (
                    "control: candidate-qualified",
                    "task: A",
                    f"generation: {proposed['generation']}",
                    f"head: {2:040x}",
                    f"tree: {2:040x}",
                    f"receipt: sha256:{'b' * 64}",
                )
            ),
        )
        ready = client.search("shared", "queue:ready")
        assert sum(str(item["role"]).startswith("review:") for item in ready) == 3
        assert sum(item["role"] == "verification" for item in ready) == 2
        scheduled = {
            item["role"]
            for worker in (f"worker-{ordinal}" for ordinal in range(6))
            for item in client.worker_snapshot("shared", worker)[2]
        }
        assert {"author", "verification"} <= scheduled
        assert any(str(role).startswith("review:") for role in scheduled)

        first = f"claim: verify:A:{2:040x}:1"
        client.add("shared", "worker-0", first)
        _verify(client, "shared", "worker-0", "A", 2, 1, preclaimed=True)
        review = client.add("shared", "worker-1", "claim: review:A:1:1")["work"]
        assert review["checks"] == []
        assert [item["state"] for item in review["acceptance_results"]] == ["passed", "pending"]
        for ordinal, worker in enumerate(("worker-1", "worker-2", "worker-3"), 1):
            if ordinal != 1:
                client.add("shared", worker, f"claim: review:A:1:{ordinal}")
            client.add(
                "shared",
                worker,
                f"approve: review:A:1:{ordinal}\nhead: {2:040x}\nverified: true\n"
                "evidence: independent candidate review passed",
            )
        client.add("shared", "worker-4", f"claim: verify:A:{2:040x}:2")
        _verify(client, "shared", "worker-4", "A", 2, 2, preclaimed=True)
        assert client.search("shared", "task:A")[0]["state"] == "merge_ready"

        restarted = WorkflowClient(JournalClient(socket), stage, client.workload_ref)
        assert not any(
            item["role"] == "verification" for item in restarted.search("shared", "queue:ready")
        )
        assert (
            _exact_count(
                tmp_path / "shared-checks.sqlite3",
                "shared",
                f"verify-pass: verify:A:{2:040x}:1",
            )
            == 1
        )
        assert (
            _exact_count(
                tmp_path / "shared-checks.sqlite3",
                "shared",
                f"verify-pass: verify:A:{2:040x}:2",
            )
            == 1
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_candidate_policy_rejection_never_exposes_reviews_or_checks(workflow) -> None:
    client, _, _ = workflow
    work = client.add("run", "worker-0", "claim: task:A")["work"]
    client.add("run", "worker-0", "checkpoint: task:A\nfiles written")
    client.add("run", "worker-0", "handoff: task:A\ncandidate pushed")
    proposed = client.add(
        "run",
        "worker-0",
        "\n".join(
            (
                "open-pr: task:A",
                f"branch: {work['branch']}",
                f"base: {1:040x}",
                f"head: {2:040x}",
                f"tree: {2:040x}",
                "verified: true",
            )
        ),
    )

    assert client.search("run", "task:A")[0]["state"] == "qualifying"
    assert not any(item["root_task_id"] == "A" for item in client.search("run", "queue:ready"))
    rejected = client.add(
        "run",
        "launcher",
        "\n".join(
            (
                "control: candidate-rejected",
                "task: A",
                f"generation: {proposed['generation']}",
                f"head: {2:040x}",
                f"tree: {2:040x}",
                f"receipt: sha256:{'d' * 64}",
                "kind: product",
                "reason: unclassified non-authoritative tooling: verification/fixtures/case.toml",
            )
        ),
    )

    assert rejected["candidate"] == "rejected"
    task = client.search("run", "task:A")[0]
    assert task["state"] == "revision"
    assert task["reviews"] == []
    assert task["qualification_receipt"] == f"sha256:{'d' * 64}"
    assert "unclassified non-authoritative tooling" in task["last_error"]
    ready = client.search("run", "queue:ready")
    assert [(item["root_task_id"], item["role"]) for item in ready] == [("A", "revision")]


def test_failed_check_is_terminal_for_candidate_and_revision_gets_new_checks(workflow) -> None:
    client, _, _ = workflow
    client.add("run", "launcher", "control: worker-capacity\nworkers: 8")
    _open_pr(client, "run", "worker-0", "A", 1, 2)
    old_claim = f"claim: verify:A:{2:040x}:1"
    client.add("run", "worker-1", old_claim)
    _verify(
        client,
        "run",
        "worker-1",
        "A",
        2,
        result="product_failure",
        preclaimed=True,
    )
    assert client.search("run", "task:A")[0]["state"] == "revision"
    assert old_claim not in {item["claim"] for item in client.search("run", "queue:ready")}
    with pytest.raises(WorkflowError, match="obsolete or unavailable"):
        client.add("run", "worker-2", old_claim)

    _open_pr(client, "run", "worker-2", "A", 3, 4)
    new_claim = f"claim: verify:A:{4:040x}:1"
    assert new_claim in {item["claim"] for item in client.search("run", "queue:ready")}
    assert old_claim not in {item["claim"] for item in client.search("run", "queue:ready")}


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
    assert len(client.search("run", "queue:ready")) == 4
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


def test_wide_product_graph_uses_seven_workers_across_implementation_review_and_merge(
    tmp_path: Path,
) -> None:
    task_ids = (
        "API",
        "DATABASE",
        "AUTH",
        "UI",
        "EMAIL",
        "PAYMENTS",
        "OBSERVABILITY",
        "API-INTEGRATION",
        "UI-INTEGRATION",
        "RELEASE",
    )
    stage = {
        "stage": "web-foundation",
        "enabled": True,
        "goal": "Build and verify an independently deployable web application.",
        "required_reviews": 3,
        "tasks": [
            {
                "id": identifier,
                "title": f"Implement {identifier}",
                "prompt": f"Implement the {identifier} product slice.",
                "depends_on": (
                    []
                    if index < 7
                    else list(task_ids[:4])
                    if identifier != "RELEASE"
                    else list(task_ids[7:9])
                ),
                "checks": [f"test {identifier}"],
            }
            for index, identifier in enumerate(task_ids)
        ],
    }
    socket = _socket()
    server, thread = serve_in_thread(tmp_path / "product.sqlite3", socket)
    client = _client(socket)
    _bootstrap(client, "product", stage)
    workers = [f"worker-{index}" for index in range(7)]
    initial = client.search("product", "queue:ready")[:7]

    def first_claim(item: dict, worker: str) -> str:
        WorkflowClient(JournalClient(socket), stage, client.workload_ref).add(
            "product", worker, item["claim"]
        )
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
            client.add("product", worker, f"checkpoint: task:{task_id}\nsaved")
            client.add("product", worker, f"handoff: task:{task_id}\nchecked")
            proposed = client.add(
                "product",
                worker,
                "\n".join(
                    (
                        f"open-pr: task:{task_id}",
                        f"branch: {item['branch']}",
                        f"base: {base:040x}",
                        f"head: {head:040x}",
                        f"tree: {head:040x}",
                        "verified: true",
                    )
                ),
            )
            client.add(
                "product",
                "launcher",
                "\n".join(
                    (
                        "control: candidate-qualified",
                        f"task: {task_id}",
                        f"generation: {proposed['generation']}",
                        f"head: {head:040x}",
                        f"tree: {head:040x}",
                        f"receipt: sha256:{'c' * 64}",
                    )
                ),
            )
        elif role.startswith("review:"):
            ordinal = int(item["review_ordinal"])
            client.add(
                "product",
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
        elif role == "verification":
            _verify(
                client,
                "product",
                worker,
                task_id,
                int(str(item["head_sha"]), 16),
                int(item["verification_ordinal"]),
                preclaimed=True,
            )
        elif role == "merge":
            client.add(
                "product",
                worker,
                f"merge: task:{task_id}\ngeneration: {item['generation']}\nhead: {item['head_sha']}",
            )
            merge = counter
            counter += 2
            client.add(
                "product",
                "launcher",
                "\n".join(
                    (
                        "control: merged",
                        f"task: {task_id}",
                        f"generation: {item['generation']}",
                        f"head: {item['head_sha']}",
                        f"merge: {merge:040x}",
                        f"tree: {merge + 1:040x}",
                        f"candidate-tree: {item['tree_sha']}",
                        "prospective-receipt:",
                    )
                ),
            )
        else:
            raise AssertionError(role)

    while client.search("product", "run:state")[0]["state"] == "running":
        progressed = False
        for worker in workers:
            active = client.search("product", f"worker:{worker}")
            if active:
                finish(worker, active[0])
                progressed = True
        for item in client.search("product", "queue:ready"):
            for worker in workers:
                try:
                    claimed_item = client.add("product", worker, item["claim"])["work"]
                except WorkflowError:
                    continue
                finish(worker, claimed_item)
                progressed = True
                break
        assert progressed
    assert client.search("product", "run:state")[0]["state"] == "publishing"
    assert all(item["state"] == "complete" for item in client.search("product", "queue:all"))
    client.add("product", "launcher", f"control: published\ncommit: {999:040x}")
    assert client.search("product", "run:state")[0]["state"] == "complete"
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
