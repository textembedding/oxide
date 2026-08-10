"""Backend-neutral ADD/SEARCH contract tests.

Set OXIDE_CONFORMANCE_JOURNAL_COMMAND to run this identical suite against an
external adapter.  With no command it exercises the disposable Python MVP.
"""

from __future__ import annotations

import os
import secrets
import shlex
from contextlib import contextmanager
from pathlib import Path

import pytest

from oxide.journal_backend import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_EXACT,
    JournalError,
    start_journal,
)
from oxide.workflow import WorkflowClient


def _command() -> list[str] | None:
    value = os.environ.get("OXIDE_CONFORMANCE_JOURNAL_COMMAND", "")
    return shlex.split(value) or None


@contextmanager
def _backend(
    root: Path,
    *,
    min_exact: int = DEFAULT_MIN_EXACT,
    max_results: int = DEFAULT_MAX_RESULTS,
):
    socket = Path("/tmp") / f"oxide-contract-{secrets.token_hex(8)}.sock"
    runtime = start_journal(
        root / "journal.store",
        socket,
        _command(),
        min_exact=min_exact,
        max_results=max_results,
    )
    try:
        yield runtime
    finally:
        runtime.close()


def _add_search_fixture(client, namespace: str) -> None:
    for ordinal in range(1, 7):
        client.add(namespace, "exact", f"exact-{ordinal}: red green blue")
    for ordinal in range(1, 13):
        client.add(namespace, "semantic", f"semantic-{ordinal}: red x green y blue")


def test_acknowledged_add_survives_restart_with_stable_identity_and_sequence(
    tmp_path: Path,
) -> None:
    database = tmp_path / "journal.store"
    socket = Path("/tmp") / f"oxide-contract-{secrets.token_hex(8)}.sock"
    first = start_journal(database, socket, _command())
    added = first.client.add("restart", "worker", "durable exact anchor")
    before = first.client.search("restart", "durable exact anchor")
    first.close()
    second = start_journal(database, socket, _command())
    try:
        after = second.client.search("restart", "durable exact anchor")
    finally:
        second.close()
    assert added["saved"] is True
    assert len(before) == len(after) == 1
    assert before[0]["stable_id"] == after[0]["stable_id"]
    assert before[0]["journal_sequence"] == after[0]["journal_sequence"]
    assert before[0]["record_id"] == after[0]["record_id"]


def test_custom_capacity_is_preserved_when_backend_restarts(tmp_path: Path) -> None:
    database = tmp_path / "journal.store"
    socket = Path("/tmp") / f"oxide-contract-{secrets.token_hex(8)}.sock"
    first = start_journal(
        database,
        socket,
        _command(),
        min_exact=2,
        max_results=3,
    )
    for ordinal in range(8):
        first.client.add("capacity-restart", "worker", f"anchor {ordinal}")
    first.close()
    second = start_journal(
        database,
        socket,
        _command(),
        min_exact=2,
        max_results=3,
    )
    try:
        found = second.client.search("capacity-restart", "anchor")
        assert (second.min_exact, second.max_results) == (2, 3)
    finally:
        second.close()
    assert [item["record_id"] for item in found] == [6, 7, 8]
    assert all(item["match_kind"] == "exact" for item in found)


def test_default_capacity_reserves_five_recent_exact_anchors(tmp_path: Path) -> None:
    with _backend(tmp_path) as runtime:
        _add_search_fixture(runtime.client, "defaults")
        found = runtime.client.search("defaults", "red green blue")
    assert (runtime.min_exact, runtime.max_results) == (5, 10)
    assert len(found) == 10
    assert [item["record_id"] for item in found] == [2, 3, 4, 5, 6, 14, 15, 16, 17, 18]
    assert sum(item["match_kind"] == "exact" for item in found) == 5


def test_custom_capacity_and_fewer_than_floor_exact_matches(tmp_path: Path) -> None:
    with _backend(tmp_path / "custom", min_exact=2, max_results=4) as runtime:
        _add_search_fixture(runtime.client, "custom")
        custom = runtime.client.search("custom", "red green blue")
    assert [item["record_id"] for item in custom] == [5, 6, 17, 18]
    assert sum(item["match_kind"] == "exact" for item in custom) == 2

    with _backend(tmp_path / "short") as runtime:
        for ordinal in range(3):
            runtime.client.add("short", "exact", f"exact-{ordinal}: red green blue")
        for ordinal in range(12):
            runtime.client.add("short", "semantic", f"semantic-{ordinal}: red x green y blue")
        short = runtime.client.search("short", "red green blue")
    assert len(short) == 10
    assert [item["record_id"] for item in short[:3]] == [1, 2, 3]
    assert sum(item["match_kind"] == "exact" for item in short) == 3


@pytest.mark.parametrize("min_exact,max_results", [(0, 10), (11, 10), (True, 10)])
def test_invalid_authoritative_capacity_fails_closed(
    tmp_path: Path, min_exact: int, max_results: int
) -> None:
    socket = Path("/tmp") / f"oxide-contract-{secrets.token_hex(8)}.sock"
    with pytest.raises(JournalError, match="1 <= min_exact <= max_results"):
        start_journal(
            tmp_path / "invalid.store",
            socket,
            _command(),
            min_exact=min_exact,
            max_results=max_results,
        )
    assert not socket.exists()


def test_score_controls_eligibility_only_while_capacity_and_order_use_sequence(
    tmp_path: Path,
) -> None:
    with _backend(tmp_path, min_exact=1, max_results=4) as runtime:
        client = runtime.client
        texts = (
            "old exact: red green blue",
            "old high semantic: red x green y blue",
            "newer low semantic: red x green",
            "below threshold: red",
            "new high semantic: red x green y blue",
            "recent exact: red green blue",
            "newest low semantic: red x green",
        )
        for text in texts:
            client.add("scores", "worker", text)
        found = client.search("scores", "red green blue")
    # The older score-1.0 semantic record loses to newer score-2/3 records.
    assert [item["record_id"] for item in found] == [3, 5, 6, 7]
    assert [item["journal_sequence"] for item in found] == sorted(
        item["journal_sequence"] for item in found
    )
    assert all(item["record_id"] != 4 for item in found)


def test_semantic_threshold_is_inclusive(tmp_path: Path) -> None:
    with _backend(tmp_path) as runtime:
        runtime.client.add("threshold", "worker", "alpha beta gamma noise")
        found = runtime.client.search("threshold", "alpha beta gamma delta epsilon")
    assert len(found) == 1
    assert found[0]["match_kind"] == "semantic"


def test_exact_and_semantic_union_is_deduplicated_and_metadata_is_public(
    tmp_path: Path,
) -> None:
    with _backend(tmp_path) as runtime:
        runtime.client.add("dedupe", "worker", "red green blue")
        found = runtime.client.search("dedupe", "red green blue")
    assert len(found) == 1
    assert found[0]["match_kind"] == "exact"
    assert {"record_id", "stable_id", "journal_sequence", "namespace"} <= set(found[0])


def test_semantic_extra_can_drive_a_follow_up_search(tmp_path: Path) -> None:
    with _backend(tmp_path) as runtime:
        client = runtime.client
        client.add("memory", "worker", "component-A implicated failure-Z")
        client.add("memory", "worker", "failure-Z repair is retry-safe")
        first = client.search("memory", "component-A failure-Z")
        assert first and first[0]["match_kind"] == "semantic"
        clue = "failure-Z" if "failure-Z" in first[0]["text"] else ""
        second = client.search("memory", clue)
    assert clue
    assert [item["record_id"] for item in second] == [1, 2]


def test_semantic_only_result_does_not_make_exact_replay_partition_nonempty(
    tmp_path: Path,
) -> None:
    replay_root = "a" * 32
    query = f"{replay_root}:"
    with _backend(tmp_path) as runtime:
        runtime.client.add(
            "empty-partition",
            "worker",
            f"oxide-routing related {replay_root} concept",
        )
        client = WorkflowClient(runtime.client, replay_root=replay_root)
        exact, returned = client._partition_records("empty-partition", "")
        replayed = client.replay_records("empty-partition")
    assert query not in returned[0]["text"]
    assert returned[0]["match_kind"] == "semantic"
    assert exact == []
    assert replayed == []


def test_large_replay_uses_one_exact_anchor_per_partition_without_wildcard_or_paging() -> None:
    replay_root = "b" * 32
    namespace = "partition-contract"
    records = [
        {
            "record_id": ordinal + 1,
            "stable_id": f"record:{ordinal + 1}",
            "journal_sequence": ordinal + 1,
            "namespace": namespace,
            "author": "worker",
            "text": "\n".join(
                (
                    f"evidence: {ordinal}",
                    f"oxide-run:{namespace}",
                    "oxide-epoch:0",
                    f"oxide-stable:{ordinal:032x}",
                    f"oxide-routing:{replay_root}:{ordinal:064b}",
                )
            ),
            "created_at": float(ordinal),
            "match_kind": "exact",
        }
        for ordinal in range(1_025)
    ]
    semantic_noise = {
        "record_id": 2_000_000,
        "stable_id": "record:semantic-noise",
        "journal_sequence": 2_000_000,
        "namespace": namespace,
        "author": "memory",
        "text": "\n".join(
            (
                f"oxide-routing related {replay_root} concept",
                f"oxide-run:{namespace}",
                "oxide-epoch:0",
                f"oxide-stable:{'f' * 32}",
                f"oxide-routing:{'c' * 32}:{1:064b}",
            )
        ),
        "created_at": 2_000_000.0,
        "match_kind": "semantic",
    }

    class OneAnchorPort:
        def __init__(self) -> None:
            self.queries: list[str] = []

        def add(self, _namespace: str, _author: str, _text: str) -> dict:
            raise AssertionError("replay must be read-only")

        def search(self, searched_namespace: str, query: str) -> list[dict]:
            assert searched_namespace == namespace
            self.queries.append(query)
            exact = [record for record in records if query in record["text"]][-1:]
            return [*exact, semantic_noise]

    backend = OneAnchorPort()
    recovered = WorkflowClient(backend, replay_root=replay_root).replay_records(namespace)  # type: ignore[arg-type]
    assert [item["journal_sequence"] for item in recovered] == list(range(1, 1_026))
    assert len({item["stable_id"] for item in recovered}) == 1_025
    assert backend.queries
    assert all(query.startswith(f"{replay_root}:") for query in backend.queries)
    assert all("*" not in query for query in backend.queries)


def test_minimum_exact_floor_replays_identical_workflow_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "journal.store"
    socket = Path("/tmp") / f"oxide-contract-{secrets.token_hex(8)}.sock"
    workload = {
        "stage": "contract",
        "enabled": True,
        "goal": "exercise replay",
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
    reference = {"schema": "OxideVerificationContractRefV1", "contract_blob": "fixture"}
    replay_root = "a" * 32
    first = start_journal(
        database,
        socket,
        _command(),
        min_exact=1,
        max_results=2,
    )
    client = WorkflowClient(
        first.client,
        workload,
        reference,
        replay_root=replay_root,
        serialization_path=tmp_path / "workflow.lock",
    )
    client.bootstrap("restart-workflow")
    client.add("restart-workflow", "worker-0", "claim: task:A")
    client.add("restart-workflow", "worker-0", "checkpoint: task:A\ndurable edit")
    before_records = client.replay_records("restart-workflow")
    before_state = client.search("restart-workflow", "task:A")
    first.close()

    second = start_journal(
        database,
        socket,
        _command(),
        min_exact=1,
        max_results=2,
    )
    try:
        restarted = WorkflowClient(
            second.client,
            workload,
            reference,
            replay_root=replay_root,
            serialization_path=tmp_path / "workflow.lock",
        )
        after_records = restarted.replay_records("restart-workflow")
        after_state = restarted.search("restart-workflow", "task:A")
    finally:
        second.close()
    assert [item["stable_id"] for item in after_records] == [
        item["stable_id"] for item in before_records
    ]
    assert [item["journal_sequence"] for item in after_records] == [
        item["journal_sequence"] for item in before_records
    ]
    assert after_state == before_state
