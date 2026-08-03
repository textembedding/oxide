from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import secrets
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .journal import Journal, JournalClient, serve_in_thread
from .workflow import WorkflowClient, WorkflowError, WorkflowReducer

ROLES = ("author", "review", "verification", "revision", "merge")
WINNER_CRASH = 86
MIN_STAGE0_ROUNDS = 4
SOURCE_PATHS = (
    "src/swarm_harness/cli.py",
    "src/swarm_harness/concurrency.py",
    "src/swarm_harness/journal.py",
    "src/swarm_harness/journal_mcp.py",
    "src/swarm_harness/worker.py",
    "src/swarm_harness/workflow.py",
)


class ConcurrencyError(RuntimeError):
    pass


def _compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_compact(value).encode()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def implementation_digest(root: Path) -> str:
    hasher = hashlib.sha256()
    for relative in SOURCE_PATHS:
        path = root / relative
        hasher.update(relative.encode())
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _stage(namespace: str) -> dict[str, Any]:
    return {
        "required_reviews": 3,
        "tasks": [
            {
                "id": "A",
                "title": "race target",
                "prompt": "validate exclusive ownership",
                "depends_on": [],
                "checks": ["true"],
                "branch": f"codex/concurrency/{namespace}",
            }
        ],
        "stage_gate": ["true"],
    }


def _bootstrap(client: WorkflowClient, namespace: str) -> None:
    client.add(
        namespace,
        "launcher",
        f"bootstrap: run:{namespace}\nstage-json: {_compact(_stage(namespace))}",
    )


def _open_pr(client: WorkflowClient, namespace: str) -> None:
    work = client.add(namespace, "setup-author", "claim: task:A")["work"]
    client.add(namespace, "setup-author", "checkpoint: task:A\nsetup edit")
    client.add(namespace, "setup-author", "handoff: task:A\nsetup checks passed")
    client.add(
        namespace,
        "setup-author",
        "\n".join(
            (
                "open-pr: task:A",
                f"branch: {work['branch']}",
                f"base: {1:040x}",
                f"head: {2:040x}",
                "verified: true",
            )
        ),
    )


def _approve(client: WorkflowClient, namespace: str, ordinal: int) -> None:
    actor = f"setup-review-{ordinal}"
    client.add(namespace, actor, f"claim: review:A:1:{ordinal}")
    client.add(
        namespace,
        actor,
        "\n".join(
            (
                f"approve: review:A:1:{ordinal}",
                f"head: {2:040x}",
                "verified: true",
                "evidence: setup verification passed",
            )
        ),
    )


def _prepare_case(client: WorkflowClient, namespace: str, role: str) -> tuple[str, str]:
    _bootstrap(client, namespace)
    if role == "author":
        return "claim: task:A", "author"
    _open_pr(client, namespace)
    if role == "review":
        return "claim: review:A:1:1", "review:specification"
    if role == "verification":
        _approve(client, namespace, 1)
        _approve(client, namespace, 2)
        return "claim: review:A:1:3", "review:integration"
    if role == "revision":
        client.add(namespace, "setup-review-1", "claim: review:A:1:1")
        client.add(
            namespace,
            "setup-review-1",
            f"challenge: review:A:1:1\nhead: {2:040x}\nverified: true\nreason: setup defect",
        )
        return "claim: task:A", "revision"
    if role == "merge":
        for ordinal in range(1, 4):
            _approve(client, namespace, ordinal)
        return "claim: merge:A:1", "merge"
    raise ConcurrencyError(f"unknown validation role: {role}")


def _projection(namespace: str, records: list[dict[str, Any]]) -> dict[str, Any]:
    view = WorkflowReducer().reduce(namespace, records)
    return {
        "state": view.state,
        "required_reviews": view.required_reviews,
        "tasks": view.tasks,
    }


def _replay(socket: str, namespace: str) -> dict[str, str | int]:
    records = JournalClient(socket).search(namespace, "*")
    journal = [
        {
            "record_id": item["record_id"],
            "namespace": item["namespace"],
            "author": item["author"],
            "text": item["text"],
            "created_at": item["created_at"],
        }
        for item in records
    ]
    return {
        "record_count": len(records),
        "journal_digest": _digest(journal),
        "workflow_digest": _digest(_projection(namespace, records)),
    }


def _wait_for(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise ConcurrencyError(f"timed out waiting for {path}")
        time.sleep(0.005)


def _child_contend(arguments: argparse.Namespace) -> int:
    client = WorkflowClient(JournalClient(arguments.socket))
    ready = client.search(arguments.namespace, "queue:ready")
    item = next((value for value in ready if value.get("claim") == arguments.claim), None)
    if item is None or item.get("role") != arguments.expected_role:
        raise ConcurrencyError("child did not observe the expected ready work")
    _write_json(
        Path(arguments.ready),
        {
            "actor": arguments.actor,
            "claim": item["claim"],
            "role": item["role"],
            "replay": _replay(arguments.socket, arguments.namespace),
        },
    )
    _wait_for(Path(arguments.start))
    time.sleep(arguments.delay)
    try:
        client.add(arguments.namespace, arguments.actor, arguments.claim)
        outcome = "accepted"
    except WorkflowError as error:
        outcome = "rejected"
        rejection = str(error)
    if outcome == "accepted" and arguments.crash_after_win:
        os._exit(WINNER_CRASH)
    if outcome == "accepted":
        client.add(arguments.namespace, arguments.actor, arguments.protected_marker)
    owned = client.search(arguments.namespace, f"worker:{arguments.actor}")
    print(
        _compact(
            {
                "actor": arguments.actor,
                "outcome": outcome,
                "rejection": rejection if outcome == "rejected" else None,
                "owned_claims": [value["claim"] for value in owned],
            }
        ),
        flush=True,
    )
    return 0


def _child_recover(arguments: argparse.Namespace) -> int:
    client = WorkflowClient(JournalClient(arguments.socket))
    owned = client.search(arguments.namespace, f"worker:{arguments.actor}")
    if len(owned) != 1 or owned[0].get("claim") != arguments.claim:
        raise ConcurrencyError("replacement did not reconstruct the winning claim")
    client.add(arguments.namespace, arguments.actor, arguments.protected_marker)
    print(_compact({"actor": arguments.actor, "outcome": "recovered"}), flush=True)
    return 0


def _child_replay(arguments: argparse.Namespace) -> int:
    time.sleep(arguments.delay)
    print(_compact({"actor": arguments.actor, **_replay(arguments.socket, arguments.namespace)}))
    return 0


def _child_argv(mode: str, **values: object) -> list[str]:
    argv = [sys.executable, "-m", "swarm_harness.concurrency", mode]
    for name, value in values.items():
        flag = "--" + name.replace("_", "-")
        if isinstance(value, bool):
            if value:
                argv.append(flag)
        else:
            argv.extend((flag, str(value)))
    return argv


def _spawn(root: Path, mode: str, **values: object) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    source = str(root / "src")
    environment["PYTHONPATH"] = source + os.pathsep + environment.get("PYTHONPATH", "")
    return subprocess.Popen(
        _child_argv(mode, **values),
        cwd=root,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _finish(process: subprocess.Popen[str], label: str, timeout: float = 30.0) -> tuple[int, str]:
    try:
        output, error = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as timeout_error:
        process.kill()
        process.communicate()
        raise ConcurrencyError(f"{label} timed out") from timeout_error
    if process.returncode not in {0, WINNER_CRASH}:
        raise ConcurrencyError(f"{label} failed ({process.returncode}): {error.strip()}")
    return int(process.returncode), output.strip()


def _record_matches(record: dict[str, Any], first_line: str) -> bool:
    return str(record["text"]).splitlines()[0] == first_line


def _run_case(
    root: Path,
    database: Path,
    socket: str,
    campaign: Path,
    role: str,
    round_index: int,
    workers: int,
    seed: int,
    crash_after_win: bool,
) -> dict[str, Any]:
    namespace = f"race-{round_index}-{role}-{seed:x}"
    case_id = f"r{round_index:03d}-{role}"
    case_dir = campaign / "cases" / case_id
    ready_dir = case_dir / "ready"
    ready_dir.mkdir(parents=True)
    start = case_dir / "start"
    client = WorkflowClient(JournalClient(socket))
    claim, expected_role = _prepare_case(client, namespace, role)
    raw = Journal(database)
    baseline = sum(_record_matches(item, claim) for item in raw.search(namespace, "*"))
    marker = f"protected-work: {case_id}\nclaim: {claim}"
    rng = random.Random(seed)
    actors = [f"worker-{index}" for index in range(workers)]
    delays = {actor: rng.random() * 0.05 for actor in actors}
    processes: dict[str, subprocess.Popen[str]] = {}
    for actor in actors:
        processes[actor] = _spawn(
            root,
            "contend",
            socket=socket,
            namespace=namespace,
            actor=actor,
            claim=claim,
            expected_role=expected_role,
            protected_marker=marker,
            ready=ready_dir / f"{actor}.json",
            start=start,
            delay=delays[actor],
            crash_after_win=crash_after_win,
        )
    deadline = time.monotonic() + 30
    while not all((ready_dir / f"{actor}.json").is_file() for actor in actors):
        failed = [actor for actor, process in processes.items() if process.poll() is not None]
        if failed or time.monotonic() >= deadline:
            for process in processes.values():
                process.kill()
            raise ConcurrencyError(f"contenders failed before the race: {failed}")
        time.sleep(0.005)
    observations = [
        json.loads((ready_dir / f"{actor}.json").read_text(encoding="utf-8")) for actor in actors
    ]
    if {item["claim"] for item in observations} != {claim}:
        raise ConcurrencyError("contenders did not observe the same claim")
    if len({_compact(item["replay"]) for item in observations}) != 1:
        raise ConcurrencyError("contenders disagreed before the claim race")
    start.write_text("go\n", encoding="utf-8")
    results: dict[str, dict[str, Any]] = {}
    crashed: list[str] = []
    for actor, process in processes.items():
        code, output = _finish(process, f"contender {actor}")
        if code == WINNER_CRASH:
            crashed.append(actor)
        elif output:
            results[actor] = json.loads(output.splitlines()[-1])
    records = raw.search(namespace, "*")
    if sum(_record_matches(item, claim) for item in records) != baseline + workers:
        raise ConcurrencyError("not every contender appended the observed claim")
    work = [
        item
        for item in client.search(namespace, "queue:all")
        if item.get("claim") == claim and item.get("state") == "working"
    ]
    if len(work) != 1 or not work[0].get("worker_id"):
        raise ConcurrencyError("claim race did not derive one effective owner")
    owner = str(work[0]["worker_id"])
    if crash_after_win:
        if crashed != [owner] or any(item["outcome"] == "accepted" for item in results.values()):
            raise ConcurrencyError("the effective winner was not the sole injected crash")
        if any(_record_matches(item, marker.splitlines()[0]) for item in records):
            raise ConcurrencyError("crashed winner performed protected work")
        recovery = _spawn(
            root,
            "recover",
            socket=socket,
            namespace=namespace,
            actor=owner,
            claim=claim,
            protected_marker=marker,
        )
        code, output = _finish(recovery, "winning-worker replacement")
        if code or json.loads(output)["outcome"] != "recovered":
            raise ConcurrencyError("winning-worker replacement failed")
    else:
        accepted = [actor for actor, item in results.items() if item["outcome"] == "accepted"]
        if accepted != [owner] or crashed:
            raise ConcurrencyError("claim responses disagree with the effective owner")
    losers = set(actors) - {owner}
    if set(results) - {owner} != losers:
        raise ConcurrencyError("one or more losing workers did not finish the claim path")
    if any(results[actor]["owned_claims"] for actor in losers):
        raise ConcurrencyError("a losing worker reconstructed protected ownership")
    protected = [
        item
        for item in raw.search(namespace, marker.splitlines()[0])
        if _record_matches(item, marker.splitlines()[0])
    ]
    if len(protected) != 1 or protected[0]["author"] != owner:
        raise ConcurrencyError("protected work was not performed exactly once by the owner")
    observer_processes = {
        actor: _spawn(
            root,
            "replay",
            socket=socket,
            namespace=namespace,
            actor=actor,
            delay=rng.random() * 0.03,
        )
        for actor in actors
    }
    replays: list[dict[str, Any]] = []
    for actor, process in observer_processes.items():
        code, output = _finish(process, f"replay {actor}")
        if code:
            raise ConcurrencyError("replay process crashed")
        replays.append(json.loads(output))
    workflow_digests = {item["workflow_digest"] for item in replays}
    journal_digests = {item["journal_digest"] for item in replays}
    if len(workflow_digests) != 1 or len(journal_digests) != 1:
        raise ConcurrencyError("workers did not converge through journal replay")
    parent_replay = _replay(socket, namespace)
    if parent_replay["workflow_digest"] not in workflow_digests:
        raise ConcurrencyError("controller replay disagreed with worker replay")
    claim_outcomes = {
        actor: (
            "crashed_after_accepted_claim" if actor in crashed else str(results[actor]["outcome"])
        )
        for actor in actors
    }
    worker_replays = {
        str(item["actor"]): {
            "record_count": item["record_count"],
            "journal_digest": item["journal_digest"],
            "workflow_digest": item["workflow_digest"],
        }
        for item in replays
    }
    return {
        "case_id": case_id,
        "role": role,
        "round": round_index,
        "seed": seed,
        "workers": workers,
        "claim": claim,
        "owner": owner,
        "claim_outcomes": claim_outcomes,
        "crash_after_win": crash_after_win,
        "crashed_worker": owner if crash_after_win else None,
        "claim_records_appended": workers,
        "protected_records": len(protected),
        "protected_author": protected[0]["author"],
        "observed_by": sorted(item["actor"] for item in observations),
        "observation_digest": observations[0]["replay"]["workflow_digest"],
        "workflow_digest": parent_replay["workflow_digest"],
        "journal_digest": parent_replay["journal_digest"],
        "worker_replays": worker_replays,
        "random_delays": delays,
    }


def run_campaign(
    root: Path,
    output_root: Path,
    *,
    workers: int,
    rounds: int,
    seed: int,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    if workers < 2 or rounds < 1:
        raise ConcurrencyError("concurrency validation requires at least 2 workers and 1 round")
    output_root.mkdir(parents=True, exist_ok=True)
    campaign = output_root / f"campaign-{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns()}"
    campaign.mkdir()
    database = campaign / "journal.sqlite3"
    socket_path = Path("/tmp") / f"swarm-concurrency-{os.getpid()}-{secrets.token_hex(4)}.sock"
    server, thread = serve_in_thread(database, socket_path)
    cases: list[dict[str, Any]] = []
    rng = random.Random(seed)
    started = time.time()
    try:
        for round_index in range(rounds):
            role_order = list(ROLES)
            rng.shuffle(role_order)
            for role in role_order:
                case_seed = rng.randrange(1, 2**63)
                crash = round_index % 2 == 0
                log(
                    f"concurrency round {round_index + 1}/{rounds} "
                    f"role={role} crash_after_win={str(crash).lower()}"
                )
                cases.append(
                    _run_case(
                        root,
                        database,
                        str(socket_path),
                        campaign,
                        role,
                        round_index,
                        workers,
                        case_seed,
                        crash,
                    )
                )
    except BaseException as error:
        _write_json(
            campaign / "report.json",
            {
                "schema": "swarm-concurrency-validation-v1",
                "status": "failed",
                "error": str(error),
                "workers": workers,
                "rounds": rounds,
                "seed": seed,
                "cases": cases,
            },
        )
        raise
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    report = {
        "schema": "swarm-concurrency-validation-v1",
        "status": "passed",
        "source_digest": implementation_digest(root),
        "workers": workers,
        "rounds": rounds,
        "seed": seed,
        "roles": list(ROLES),
        "case_count": len(cases),
        "winner_crash_cases": sum(item["crash_after_win"] for item in cases),
        "started_at": started,
        "completed_at": time.time(),
        "invariants": {
            "same_claim_observed": True,
            "one_effective_owner": True,
            "losers_did_no_protected_work": True,
            "winner_crash_recovered_by_replay": True,
            "all_worker_replays_identical": True,
        },
        "cases": cases,
        "report_path": str((campaign / "report.json").resolve()),
    }
    report["receipt_digest"] = _digest(report)
    _write_json(campaign / "report.json", report)
    _write_json(output_root / "latest.json", report)
    return report


def validate_receipt(
    root: Path,
    receipt_path: Path,
    *,
    required_workers: int,
    minimum_rounds: int = MIN_STAGE0_ROUNDS,
    require_current_source: bool = True,
) -> dict[str, Any]:
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConcurrencyError(
            "Stage 0 requires a passing concurrency campaign; run "
            "./swarmctl harness validate-concurrency --workers 7"
        ) from error
    report_path = Path(str(receipt.get("report_path", "")))
    try:
        archived = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        archived = None
    unsealed = dict(receipt)
    claimed_digest = unsealed.pop("receipt_digest", None)
    required_invariants = {
        "same_claim_observed",
        "one_effective_owner",
        "losers_did_no_protected_work",
        "winner_crash_recovered_by_replay",
        "all_worker_replays_identical",
    }
    if (
        receipt.get("schema") != "swarm-concurrency-validation-v1"
        or receipt.get("status") != "passed"
        or require_current_source
        and receipt.get("source_digest") != implementation_digest(root)
        or int(receipt.get("workers", 0)) < required_workers
        or int(receipt.get("rounds", 0)) < minimum_rounds
        or set(receipt.get("roles", [])) != set(ROLES)
        or not all(receipt.get("invariants", {}).get(name) is True for name in required_invariants)
        or claimed_digest != _digest(unsealed)
        or archived != receipt
    ):
        raise ConcurrencyError(
            "Stage 0 concurrency receipt is absent, stale, or insufficient; run "
            f"./swarmctl harness validate-concurrency --workers {required_workers}"
        )
    return receipt


def _child_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="mode", required=True)
    contend = commands.add_parser("contend")
    for name in (
        "socket",
        "namespace",
        "actor",
        "claim",
        "expected-role",
        "protected-marker",
        "ready",
        "start",
    ):
        contend.add_argument("--" + name, required=True)
    contend.add_argument("--delay", type=float, required=True)
    contend.add_argument("--crash-after-win", action="store_true")
    contend.set_defaults(handler=_child_contend)
    recover = commands.add_parser("recover")
    for name in ("socket", "namespace", "actor", "claim", "protected-marker"):
        recover.add_argument("--" + name, required=True)
    recover.set_defaults(handler=_child_recover)
    replay = commands.add_parser("replay")
    for name in ("socket", "namespace", "actor"):
        replay.add_argument("--" + name, required=True)
    replay.add_argument("--delay", type=float, required=True)
    replay.set_defaults(handler=_child_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = _child_parser().parse_args(argv)
        return int(arguments.handler(arguments))
    except (ConcurrencyError, OSError, ValueError) as error:
        print(f"swarm-concurrency: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
