import fcntl
import hashlib
import json
import re
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .journal_backend import JournalPort


class WorkflowError(RuntimeError):
    pass


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_AUTHOR_CLAIM = re.compile(r"^claim: task:([^\s:]+)$")
_REVIEW_CLAIM = re.compile(r"^claim: review:([^\s:]+):(\d+):(\d+)$")
_MERGE_CLAIM = re.compile(r"^claim: merge:([^\s:]+):(\d+)$")
_VERIFY_CLAIM = re.compile(r"^claim: verify:([^\s:]+):([0-9a-f]{40}):(\d+)$")
_AUTHOR_MARKER = re.compile(r"^(checkpoint|handoff|open-pr): task:([^\s:]+)$")
_REVIEW_DECISION = re.compile(r"^(approve|challenge): review:([^\s:]+):(\d+):(\d+)$")
_MERGE_REQUEST = re.compile(r"^merge: task:([^\s:]+)$")
_VERIFY_RESULT = re.compile(r"^(verify-pass|verify-fail): verify:([^\s:]+):([0-9a-f]{40}):(\d+)$")
_BLOCKED = re.compile(r"^(blocked|blocker): task:([^\s:]+)$")
_RECLAIM = re.compile(r"^control: reclaim worker:([^\s:]+)$")
_REVIEW_ROLES = ("specification", "adversarial", "integration")
_REPLAY_FANOUT = "01"
_REPLAY_WIDTH = 64
_ROUTING = re.compile(r"^swarm-routing:([0-9a-f]{32}):([01]{64})$")
_RUN = re.compile(r"^swarm-run:(.+)$")
_EPOCH = re.compile(r"^swarm-epoch:(\d+)$")
_STABLE = re.compile(r"^swarm-stable:([0-9a-f]{32})$")


def _compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _line_value(text: str, name: str) -> str | None:
    prefix = name + ":"
    for line in text.splitlines()[1:]:
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def _record_text(record: dict[str, Any]) -> str:
    """Return workflow content without the private replay-routing suffix."""

    lines = str(record["text"]).splitlines()
    while lines and (
        _ROUTING.fullmatch(lines[-1])
        or _RUN.fullmatch(lines[-1])
        or _EPOCH.fullmatch(lines[-1])
        or _STABLE.fullmatch(lines[-1])
    ):
        lines.pop()
    return "\n".join(lines)


def _record_route(record: dict[str, Any]) -> dict[str, Any] | None:
    values: dict[str, Any] = {}
    for line in str(record.get("text", "")).splitlines():
        if match := _ROUTING.fullmatch(line):
            values.update(replay_root=match.group(1), replay_id=match.group(2))
        elif match := _RUN.fullmatch(line):
            values["run_id"] = match.group(1)
        elif match := _EPOCH.fullmatch(line):
            values["epoch"] = int(match.group(1))
        elif match := _STABLE.fullmatch(line):
            values["stable_id"] = match.group(1)
    required = {"replay_root", "replay_id", "run_id", "epoch", "stable_id"}
    return values if required <= set(values) else None


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()


@dataclass
class Projection:
    namespace: str
    records: list[dict[str, Any]]
    state: str = "uninitialized"
    stage: dict[str, Any] | None = None
    stage_bytes: str | None = None
    workload_ref: dict[str, Any] | None = None
    epoch: int = 0
    required_reviews: int = 0
    worker_capacity: int = 0
    frontier_sha: str | None = None
    drain_reviews: bool = False
    terminal_blockers: bool = False
    worker_verification: bool = False
    reusable_slots: bool = False
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    verifications: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcomes: dict[int, tuple[bool, dict[str, Any] | str]] = field(default_factory=dict)

    def accept(self, record: dict[str, Any], value: dict[str, Any]) -> None:
        self.outcomes[int(record["record_id"])] = (True, value)

    def reject(self, record: dict[str, Any], reason: str) -> None:
        self.outcomes[int(record["record_id"])] = (False, reason)


class WorkflowReducer:
    def __init__(
        self,
        workload: dict[str, Any] | None = None,
        workload_ref: dict[str, Any] | None = None,
        *,
        epoch: int = 0,
        history_sequence: int = 0,
        epoch_frontiers: list[dict[str, int]] | None = None,
    ) -> None:
        self.workload = workload
        self.workload_ref = workload_ref
        self.epoch = epoch
        self.history_sequence = history_sequence
        self.epoch_frontiers = [
            (int(item["epoch"]), int(item["through"])) for item in (epoch_frontiers or [])
        ]
        if not self.epoch_frontiers and epoch > 0 and history_sequence > 0:
            # Compatibility for an in-memory client representing a single prior
            # rewind. Persisted schema-v6 runs always carry explicit frontiers.
            self.epoch_frontiers = [(epoch - 1, history_sequence)]

    def _epoch_is_authoritative(self, record_epoch: int, sequence: int) -> bool:
        lower = 0
        for epoch, through in self.epoch_frontiers:
            if sequence <= through:
                return sequence > lower and record_epoch == epoch
            lower = through
        return sequence > self.history_sequence and record_epoch == self.epoch

    def reduce(self, namespace: str, records: list[dict[str, Any]]) -> Projection:
        view = Projection(namespace=namespace, records=records, epoch=self.epoch)
        for record in records:
            self._apply(view, record)
        return view

    def _apply(self, view: Projection, record: dict[str, Any]) -> None:
        route = _record_route(record)
        try:
            sequence = int(record["journal_sequence"])
        except (KeyError, TypeError, ValueError):
            view.reject(record, "workflow record is missing a stable journal sequence")
            return
        if (
            route is None
            or record.get("namespace") != view.namespace
            or route["run_id"] != view.namespace
        ):
            view.reject(record, "workflow record routing metadata is invalid")
            return
        if not self._epoch_is_authoritative(int(route["epoch"]), sequence):
            view.reject(record, "workflow record carries a stale run epoch")
            return
        text = _record_text(record)
        author = str(record["author"])
        first = text.splitlines()[0]
        if first.startswith("bootstrap:"):
            self._bootstrap(view, record)
            return
        if view.stage is None:
            view.reject(record, "workflow is not bootstrapped")
            return
        if first == "control: worker-capacity":
            raw_capacity = _line_value(text, "workers") or ""
            if author != "launcher" or not raw_capacity.isdigit():
                view.reject(record, "only the launcher may configure worker capacity")
            else:
                capacity = int(raw_capacity)
                if not 1 <= capacity <= 64:
                    view.reject(record, "worker capacity must be 1..64")
                else:
                    saved = capacity != view.worker_capacity
                    view.worker_capacity = capacity
                    view.terminal_blockers |= _line_value(text, "terminal-blockers") == "true"
                    view.accept(record, {"saved": saved, "workers": capacity})
            return
        if first == "control: drain-reviews":
            if author != "launcher":
                view.reject(record, "review-drain activation requires the launcher")
            else:
                saved = not view.drain_reviews
                view.drain_reviews = True
                view.accept(record, {"saved": saved, "review_drain": True})
            return
        if first == "control: worker-verification":
            if author != "launcher":
                view.reject(record, "worker verification activation requires the launcher")
            else:
                saved = not view.worker_verification
                view.worker_verification = True
                for task in view.tasks.values():
                    stale_launcher_block = task["state"] == "blocked" and str(
                        task["last_error"] or ""
                    ).lower().startswith("merge launcher")
                    if task["state"] in {"reviewing", "merge_ready"} or stale_launcher_block:
                        task["verification_required"] = True
                        if stale_launcher_block:
                            task.update(state="reviewing", last_error=None)
                            view.verifications = {
                                key: item
                                for key, item in view.verifications.items()
                                if item["task_id"] != task["task_id"]
                            }
                        self._advance_candidate(view, task)
                view.accept(record, {"saved": saved, "worker_verification": True})
            return
        if first == "control: reusable-slots":
            if author != "launcher":
                view.reject(record, "only the launcher may enable reusable worker slots")
            else:
                saved = not view.reusable_slots
                view.reusable_slots = True
                view.accept(record, {"saved": saved, "reusable_slots": True})
            return
        if first == "control: pause":
            if author != "launcher" or view.state == "complete":
                view.reject(record, "pause is unavailable")
            else:
                view.state = "paused"
                view.accept(record, {"saved": True, "state": "paused"})
            return
        if first == "control: resume":
            if author != "launcher" or view.state == "complete":
                view.reject(record, "resume is unavailable")
            else:
                view.state = (
                    "publishing"
                    if all(task["state"] == "complete" for task in view.tasks.values())
                    else "running"
                )
                view.accept(record, {"saved": True, "state": view.state})
            return
        if first == "control: merged":
            self._merge_result(view, record, failed=False)
            return
        if first == "control: merge-failed":
            self._merge_result(view, record, failed=True)
            return
        if first == "control: published":
            commit = _line_value(text, "commit") or ""
            if author != "launcher" or view.state != "publishing" or not _COMMIT.fullmatch(commit):
                view.reject(record, "publication is not authorized")
            else:
                view.state = "complete"
                view.accept(record, {"saved": True, "state": "complete", "commit": commit})
            return
        if view.state != "running":
            view.reject(record, f"run is {view.state}")
            return
        if match := _AUTHOR_CLAIM.fullmatch(first):
            self._claim_author(view, record, match.group(1))
        elif match := _REVIEW_CLAIM.fullmatch(first):
            self._claim_review(
                view, record, match.group(1), int(match.group(2)), int(match.group(3))
            )
        elif match := _MERGE_CLAIM.fullmatch(first):
            self._claim_merge(view, record, match.group(1), int(match.group(2)))
        elif match := _VERIFY_CLAIM.fullmatch(first):
            self._claim_verification(
                view, record, match.group(1), match.group(2), int(match.group(3))
            )
        elif match := _AUTHOR_MARKER.fullmatch(first):
            action, task_id = match.groups()
            self._author_record(view, record, action, task_id)
        elif match := _REVIEW_DECISION.fullmatch(first):
            self._review_decision(
                view,
                record,
                match.group(1),
                match.group(2),
                int(match.group(3)),
                int(match.group(4)),
            )
        elif match := _MERGE_REQUEST.fullmatch(first):
            self._merge_request(view, record, match.group(1))
        elif match := _VERIFY_RESULT.fullmatch(first):
            self._verification_result(
                view,
                record,
                match.group(1),
                match.group(2),
                match.group(3),
                int(match.group(4)),
            )
        elif match := _BLOCKED.fullmatch(first):
            if match.group(1) != "blocked":
                view.reject(record, "blocker records must start with exact `blocked: task:<id>`")
            elif view.terminal_blockers:
                self._block_author(view, record, match.group(2))
            else:
                view.accept(record, {"saved": True, "record_id": record["record_id"]})
        elif match := _RECLAIM.fullmatch(first):
            self._reclaim(view, record, match.group(1))
        else:
            view.accept(record, {"saved": True, "record_id": record["record_id"]})

    @staticmethod
    def _reclaim(view: Projection, record: dict[str, Any], worker: str) -> None:
        if record["author"] != "launcher":
            view.reject(record, "only the launcher may reclaim a crashed worker")
            return
        recovered: str | None = None
        for task in view.tasks.values():
            if task["state"] == "authoring" and task["worker_id"] == worker:
                task.update(
                    state="revision" if task["generation"] else "pending",
                    worker_id=None,
                    checkpoint=False,
                    handoff=False,
                )
                recovered = task["task_id"]
            elif task["state"] == "merge_claimed" and task["merge_worker_id"] == worker:
                task.update(state="merge_ready", merge_worker_id=None)
                recovered = f"{task['task_id']}/merge"
            for review in task["reviews"]:
                if review["state"] == "claimed" and review["worker_id"] == worker:
                    review.update(state="pending", worker_id=None)
                    recovered = f"{task['task_id']}/review-{review['ordinal']}"
        for key, verification in list(view.verifications.items()):
            if verification["state"] == "claimed" and verification["worker_id"] == worker:
                del view.verifications[key]
                recovered = f"{verification['task_id']}/verify-{verification['check_ordinal']}"
        view.accept(record, {"saved": recovered is not None, "reclaimed": recovered})

    def _bootstrap(self, view: Projection, record: dict[str, Any]) -> None:
        text = _record_text(record)
        expected = f"bootstrap: run:{view.namespace}"
        raw_ref = _line_value(text, "workload-ref")
        if (
            record["author"] != "launcher"
            or text.splitlines()[0] != expected
            or raw_ref is None
            or self.workload is None
            or self.workload_ref is None
        ):
            view.reject(record, "launcher bootstrap requires the frozen repository workload")
            return
        try:
            recorded_ref = json.loads(raw_ref)
        except json.JSONDecodeError:
            view.reject(record, "workload-ref is invalid")
            return
        if recorded_ref != self.workload_ref:
            view.reject(record, "bootstrap workload reference does not match the frozen run")
            return
        stage = self.workload
        canonical = _compact(stage)
        if view.stage is not None:
            if recorded_ref != view.workload_ref:
                view.reject(record, "workflow was bootstrapped with a different workload reference")
            else:
                view.accept(record, {"saved": False, "state": view.state})
            return
        tasks = stage.get("tasks") if isinstance(stage, dict) else None
        required = stage.get("required_reviews") if isinstance(stage, dict) else None
        if (
            not isinstance(tasks, list)
            or not tasks
            or not isinstance(required, int)
            or not 1 <= required <= 16
        ):
            view.reject(record, "stage requires tasks and 1..16 internal reviews")
            return
        identifiers = {str(task.get("id", "")) for task in tasks if isinstance(task, dict)}
        if "" in identifiers or len(identifiers) != len(tasks):
            view.reject(record, "task identifiers must be unique and nonempty")
            return
        normalized: list[dict[str, Any]] = []
        for ordinal, source in enumerate(tasks):
            dependencies = source.get("depends_on", [])
            checks = source.get("checks", [])
            branch = str(
                source.get("branch")
                or f"codex/swarm-{_slug(view.namespace)}/{_slug(str(source['id']))}"
            )
            if (
                not isinstance(dependencies, list)
                or not all(isinstance(item, str) and item for item in dependencies)
                or not set(dependencies) <= identifiers
                or source["id"] in dependencies
                or not isinstance(checks, list)
                or not checks
                or not all(isinstance(item, str) and item for item in checks)
                or _BRANCH.fullmatch(branch) is None
                or ".." in branch
            ):
                view.reject(record, "task graph, checks, or branch is invalid")
                return
            normalized.append(
                {
                    "task_id": str(source["id"]),
                    "ordinal": ordinal,
                    "title": str(source.get("title", source["id"])),
                    "prompt": str(source.get("prompt", "")),
                    "depends_on": list(dependencies),
                    "checks": list(checks),
                    "branch": branch,
                    "state": "pending",
                    "worker_id": None,
                    "author_id": None,
                    "base_sha": None,
                    "head_sha": None,
                    "generation": 0,
                    "merge_worker_id": None,
                    "merged_sha": None,
                    "last_error": None,
                    "checkpoint": False,
                    "handoff": False,
                    "reviews": [],
                    "verification_required": False,
                }
            )
        stage_gate = stage.get("stage_gate", [])
        if (
            not isinstance(stage_gate, list)
            or not stage_gate
            or not all(isinstance(item, str) and item for item in stage_gate)
        ):
            view.reject(record, "stage gate must contain commands")
            return
        remaining = {task["task_id"]: set(task["depends_on"]) for task in normalized}
        while remaining:
            ready = {task_id for task_id, dependencies in remaining.items() if not dependencies}
            if not ready:
                view.reject(record, "task dependencies contain a cycle")
                return
            remaining = {
                task_id: dependencies - ready
                for task_id, dependencies in remaining.items()
                if task_id not in ready
            }
        view.stage = stage
        view.stage_bytes = canonical
        view.workload_ref = dict(recorded_ref)
        view.required_reviews = required
        view.tasks = {task["task_id"]: task for task in normalized}
        view.state = "running"
        view.accept(record, {"saved": True, "state": "running"})

    @staticmethod
    def _dependencies_complete(view: Projection, task: dict[str, Any]) -> bool:
        return all(view.tasks[item]["state"] == "complete" for item in task["depends_on"])

    @staticmethod
    def _task(view: Projection, task_id: str) -> dict[str, Any] | None:
        return view.tasks.get(task_id)

    @staticmethod
    def _owned(view: Projection, worker: str) -> bool:
        for task in view.tasks.values():
            if task["state"] == "authoring" and task["worker_id"] == worker:
                return True
            if task["state"] == "merge_claimed" and task["merge_worker_id"] == worker:
                return True
            if any(
                review["state"] == "claimed" and review["worker_id"] == worker
                for review in task["reviews"]
            ):
                return True
        return any(
            verification["state"] == "claimed" and verification["worker_id"] == worker
            for verification in view.verifications.values()
        )

    def _claim_author(self, view: Projection, record: dict[str, Any], task_id: str) -> None:
        task = self._task(view, task_id)
        worker = str(record["author"])
        if task is None:
            view.reject(record, "unknown task")
        elif task["state"] == "authoring" and task["worker_id"] == worker:
            view.accept(
                record, {"saved": False, "claim": "resumed", "work": self._author_work(view, task)}
            )
        elif task["state"] == "authoring":
            view.reject(record, "task is already claimed")
        elif task["state"] not in {"pending", "revision"}:
            view.reject(record, f"task is {task['state']}")
        elif not self._dependencies_complete(view, task):
            view.reject(record, "task dependencies are not complete")
        elif self._owned(view, worker):
            view.reject(record, "worker already owns work")
        else:
            if task["state"] == "revision":
                view.verifications = {
                    key: item
                    for key, item in view.verifications.items()
                    if item["task_id"] != task_id or item["state"] in {"claimed", "obsolete"}
                }
            task.update(
                state="authoring",
                worker_id=worker,
                author_id=worker,
                checkpoint=False,
                handoff=False,
            )
            view.accept(
                record, {"saved": True, "claim": "accepted", "work": self._author_work(view, task)}
            )

    def _claim_review(
        self, view: Projection, record: dict[str, Any], task_id: str, generation: int, ordinal: int
    ) -> None:
        task = self._task(view, task_id)
        worker = str(record["author"])
        review = (
            next(
                (
                    item
                    for item in task["reviews"]
                    if item["generation"] == generation and item["ordinal"] == ordinal
                ),
                None,
            )
            if task
            else None
        )
        if (
            task is None
            or review is None
            or task["state"] != "reviewing"
            or task["generation"] != generation
        ):
            view.reject(record, "review is obsolete or unavailable")
        elif review["state"] == "claimed" and review["worker_id"] == worker:
            view.accept(
                record,
                {
                    "saved": False,
                    "claim": "resumed",
                    "work": self._review_work(view, task, review),
                },
            )
        elif review["state"] != "pending":
            view.reject(
                record,
                "review slot is already claimed"
                if review["state"] == "claimed"
                else f"review is {review['state']}",
            )
        elif not view.reusable_slots and worker == task["author_id"]:
            view.reject(record, "an author cannot review its own candidate")
        elif self._owned(view, worker):
            view.reject(record, "worker already owns work")
        elif not view.reusable_slots and any(
            item["worker_id"] == worker and item["state"] in {"claimed", "approved", "challenged"}
            for item in task["reviews"]
            if item["generation"] == generation
        ):
            view.reject(record, "reviewers must be distinct for a candidate")
        else:
            review.update(state="claimed", worker_id=worker)
            view.accept(
                record,
                {
                    "saved": True,
                    "claim": "accepted",
                    "work": self._review_work(view, task, review),
                },
            )

    def _claim_merge(
        self, view: Projection, record: dict[str, Any], task_id: str, generation: int
    ) -> None:
        task = self._task(view, task_id)
        worker = str(record["author"])
        if task is None:
            view.reject(record, "unknown task")
        elif task["state"] == "merge_claimed" and task["merge_worker_id"] == worker:
            view.accept(
                record,
                {"saved": False, "claim": "resumed", "work": self._merge_work(view, task)},
            )
        elif task["state"] != "merge_ready" or task["generation"] != generation:
            view.reject(record, "merge is obsolete or unavailable")
        elif self._owned(view, worker):
            view.reject(record, "worker already owns work")
        else:
            task.update(state="merge_claimed", merge_worker_id=worker)
            view.accept(
                record,
                {"saved": True, "claim": "accepted", "work": self._merge_work(view, task)},
            )

    @staticmethod
    def _verification_key(task_id: str, frontier: str, check_ordinal: int) -> str:
        return f"{task_id}:{frontier}:{check_ordinal}"

    @staticmethod
    def _verification_head(view: Projection, task: dict[str, Any]) -> str | None:
        if not task["verification_required"] or task["state"] == "complete":
            # Verification is a pre-merge acceptance gate. A completed task has
            # already crossed it and must never return to the work queue.
            return None
        if task["state"] in {"reviewing", "merge_ready", "merge_claimed", "merge_requested"}:
            return task["head_sha"]
        return None

    def _claim_verification(
        self,
        view: Projection,
        record: dict[str, Any],
        task_id: str,
        frontier: str,
        check_ordinal: int,
    ) -> None:
        task = self._task(view, task_id)
        worker = str(record["author"])
        key = self._verification_key(task_id, frontier, check_ordinal)
        verification = view.verifications.get(key)
        if (
            not view.worker_capacity
            or task is None
            or frontier != self._verification_head(view, task)
            or not 1 <= check_ordinal <= len(task["checks"])
        ):
            view.reject(record, "verification is obsolete or unavailable")
        elif (
            verification is not None
            and verification["state"] == "claimed"
            and verification["worker_id"] == worker
        ):
            work = self._verification_work(view, task, verification)
            view.accept(record, {"saved": False, "claim": "resumed", "work": work})
        elif not view.reusable_slots and worker == task["author_id"]:
            view.reject(record, "an author cannot independently verify its own task")
        elif self._owned(view, worker):
            view.reject(record, "worker already owns work")
        elif verification is not None and (
            task["state"] != "authoring" or verification["state"] == "claimed"
        ):
            view.reject(record, f"verification is {verification['state']}")
        else:
            if verification is None:
                verification = {
                    "task_id": task_id,
                    "frontier": frontier,
                    "check_ordinal": check_ordinal,
                    "state": "claimed",
                    "worker_id": worker,
                    "result": None,
                    "detail": None,
                }
            else:
                verification.update(state="claimed", worker_id=worker, result=None, detail=None)
            view.verifications[key] = verification
            work = self._verification_work(view, task, verification)
            view.accept(record, {"saved": True, "claim": "accepted", "work": work})

    def _author_record(
        self, view: Projection, record: dict[str, Any], action: str, task_id: str
    ) -> None:
        task = self._task(view, task_id)
        worker = str(record["author"])
        if task is None or task["state"] != "authoring" or task["worker_id"] != worker:
            view.reject(record, "author record does not belong to the current owner")
            return
        if action == "checkpoint":
            task["checkpoint"] = True
            view.accept(record, {"saved": True, "record_id": record["record_id"]})
        elif action == "handoff":
            task["handoff"] = True
            view.accept(record, {"saved": True, "record_id": record["record_id"]})
        else:
            self._open_pr(view, record, task)

    def _block_author(self, view: Projection, record: dict[str, Any], task_id: str) -> None:
        task = self._task(view, task_id)
        text = _record_text(record)
        expected_role = "revision" if task and task["generation"] else "author"
        reason = _line_value(text, "reason")
        exact = task and {
            "role": expected_role,
            "branch": task["branch"],
            "generation": str(task["generation"]),
            "head": str(task["head_sha"] or ""),
            "verified": "false",
        }
        if not exact or task["state"] != "authoring" or task["worker_id"] != record["author"]:
            view.reject(record, "blocker is not bound to the owned exact author assignment")
            return
        if not reason or any(_line_value(text, key) != value for key, value in exact.items()):
            view.reject(record, "blocker is not bound to the owned exact author assignment")
            return
        task.update(
            state="blocked",
            worker_id=None,
            checkpoint=False,
            handoff=False,
            last_error=str(reason)[:2000],
        )
        view.accept(record, {"saved": True, "blocked": task_id, "state": "blocked"})

    def _open_pr(self, view: Projection, record: dict[str, Any], task: dict[str, Any]) -> None:
        text = _record_text(record)
        branch = _line_value(text, "branch") or ""
        base = _line_value(text, "base") or ""
        head = _line_value(text, "head") or ""
        if (
            branch != task["branch"]
            or not _COMMIT.fullmatch(base)
            or not _COMMIT.fullmatch(head)
            or base == head
            or _line_value(text, "verified") != "true"
            or not task["checkpoint"]
            or not task["handoff"]
        ):
            view.reject(
                record, "PR requires checkpoint, handoff, exact branch/base/head, and verified:true"
            )
            return
        generation = int(task["generation"]) + 1
        task["reviews"] = [
            {
                "generation": generation,
                "ordinal": ordinal,
                "role": (
                    _REVIEW_ROLES[ordinal - 1]
                    if ordinal <= len(_REVIEW_ROLES)
                    else f"review-{ordinal}"
                ),
                "state": "pending",
                "worker_id": None,
                "head_sha": head,
            }
            for ordinal in range(1, view.required_reviews + 1)
        ]
        task.update(
            state="reviewing",
            worker_id=None,
            base_sha=base,
            head_sha=head,
            generation=generation,
            merge_worker_id=None,
            last_error=None,
            verification_required=view.worker_verification,
        )
        view.accept(
            record,
            {
                "saved": True,
                "pr": "opened",
                "task_id": task["task_id"],
                "generation": generation,
                "head": head,
                "required_reviews": view.required_reviews,
            },
        )

    def _advance_candidate(self, view: Projection, task: dict[str, Any]) -> None:
        if not task["verification_required"] or task["state"] not in {"reviewing", "merge_ready"}:
            return
        reviews_done = all(item["state"] in {"approved", "challenged"} for item in task["reviews"])
        head = str(task["head_sha"])
        checks = [
            view.verifications.get(self._verification_key(task["task_id"], head, ordinal))
            for ordinal in range(1, len(task["checks"]) + 1)
        ]
        checks_done = all(item and item["state"] in {"passed", "failed"} for item in checks)
        candidate_failed = any(item["state"] == "challenged" for item in task["reviews"]) or any(
            item and item["state"] == "failed" for item in checks
        )
        if candidate_failed:
            for review in task["reviews"]:
                if review["state"] in {"pending", "claimed"}:
                    review["state"] = "obsolete"
            for item in checks:
                if item and item["state"] in {"pending", "claimed"}:
                    item["state"] = "obsolete"
            task.update(state="revision", worker_id=None, merge_worker_id=None)
        elif not reviews_done or not checks_done:
            task["state"] = "reviewing"
        else:
            task["state"] = "merge_ready"

    def _review_decision(
        self,
        view: Projection,
        record: dict[str, Any],
        decision: str,
        task_id: str,
        generation: int,
        ordinal: int,
    ) -> None:
        task = self._task(view, task_id)
        worker = str(record["author"])
        review = (
            next(
                (
                    item
                    for item in task["reviews"]
                    if item["generation"] == generation and item["ordinal"] == ordinal
                ),
                None,
            )
            if task
            else None
        )
        text = _record_text(record)
        head = _line_value(text, "head") or ""
        detail = _line_value(text, "evidence" if decision == "approve" else "reason")
        exact_owner_result = (
            review is not None
            and review["worker_id"] == worker
            and head == review["head_sha"]
            and _line_value(text, "verified") == "true"
            and bool(detail)
        )
        if exact_owner_result and review["state"] == "obsolete":
            view.accept(
                record,
                {"saved": True, "decision": decision, "candidate": "obsolete"},
            )
            return
        if (
            task is None
            or review is None
            or task["state"] != "reviewing"
            or task["generation"] != generation
            or review["state"] != "claimed"
            or review["worker_id"] != worker
            or head != task["head_sha"]
            or head != review["head_sha"]
            or _line_value(text, "verified") != "true"
            or not detail
        ):
            view.reject(record, "review decision is not bound to the owned current candidate")
            return
        if view.drain_reviews or task["verification_required"]:
            review["state"] = "challenged" if decision == "challenge" else "approved"
            if decision == "challenge":
                task["last_error"] = str(detail)[:2000]
            approvals = sum(item["state"] == "approved" for item in task["reviews"])
            if task["verification_required"]:
                self._advance_candidate(view, task)
            elif all(item["state"] in {"approved", "challenged"} for item in task["reviews"]):
                task.update(
                    state=(
                        "revision"
                        if any(item["state"] == "challenged" for item in task["reviews"])
                        else "merge_ready"
                    ),
                    worker_id=None,
                    merge_worker_id=None,
                )
            view.accept(record, {"saved": True, "decision": decision, "approvals": approvals})
            return
        if decision == "challenge":
            review["state"] = "challenged"
            for item in task["reviews"]:
                if item["state"] in {"pending", "claimed"}:
                    item["state"] = "obsolete"
            task.update(
                state="revision",
                worker_id=None,
                merge_worker_id=None,
                last_error=(
                    _line_value(text, "reason") or "internal reviewer challenged the candidate"
                )[:2000],
            )
            view.accept(record, {"saved": True, "decision": "challenge"})
            return
        review["state"] = "approved"
        approvals = sum(item["state"] == "approved" for item in task["reviews"])
        if approvals == view.required_reviews:
            task["state"] = "merge_ready"
        view.accept(record, {"saved": True, "decision": "approve", "approvals": approvals})

    def _verification_result(
        self,
        view: Projection,
        record: dict[str, Any],
        result: str,
        task_id: str,
        frontier: str,
        check_ordinal: int,
    ) -> None:
        key = self._verification_key(task_id, frontier, check_ordinal)
        verification = view.verifications.get(key)
        text = _record_text(record)
        detail_name = "evidence" if result == "verify-pass" else "reason"
        detail = _line_value(text, detail_name)
        exact_owner_result = (
            verification is not None
            and verification["worker_id"] == record["author"]
            and _line_value(text, "head") == frontier
            and _line_value(text, "verified") == "true"
            and bool(detail)
        )
        state = "passed" if result == "verify-pass" else "failed"
        if exact_owner_result and verification["state"] == "obsolete":
            # The claim was valid when work began, but another independent
            # result invalidated the candidate first. Preserve this completed
            # observation as immutable audit evidence without advancing state.
            verification.update(state="observed", result=state, detail=str(detail)[:2000])
            view.accept(
                record,
                {"saved": True, "verification": state, "candidate": "obsolete"},
            )
            return
        if not exact_owner_result or verification["state"] != "claimed":
            view.reject(record, "verification result is not bound to the owned exact frontier")
            return
        verification.update(state=state, result=state, detail=str(detail)[:2000])
        task = self._task(view, task_id)
        if task is not None and task["verification_required"]:
            if state == "failed":
                task["last_error"] = str(detail)[:2000]
            self._advance_candidate(view, task)
        view.accept(record, {"saved": True, "verification": state})

    def _merge_request(self, view: Projection, record: dict[str, Any], task_id: str) -> None:
        task = self._task(view, task_id)
        text = _record_text(record)
        generation = _line_value(text, "generation") or ""
        head = _line_value(text, "head") or ""
        if (
            task is None
            or task["state"] != "merge_claimed"
            or task["merge_worker_id"] != record["author"]
            or generation != str(task["generation"])
            or head != task["head_sha"]
            or sum(item["state"] == "approved" for item in task["reviews"]) != view.required_reviews
        ):
            view.reject(record, "merge request is not bound to the owned fully approved PR")
        else:
            task["state"] = "merge_requested"
            view.accept(record, {"saved": True, "merge": "requested"})

    def _merge_result(self, view: Projection, record: dict[str, Any], failed: bool) -> None:
        text = _record_text(record)
        task = self._task(view, _line_value(text, "task") or "")
        if (
            record["author"] != "launcher"
            or task is None
            or task["state"] != "merge_requested"
            or _line_value(text, "generation") != str(task["generation"])
            or _line_value(text, "head") != task["head_sha"]
        ):
            view.reject(record, "merge result does not match the requested PR")
            return
        if failed:
            task.update(
                state="revision",
                worker_id=None,
                merge_worker_id=None,
                last_error=(
                    _line_value(text, "reason") or "mechanical merge or acceptance check failed"
                )[:2000],
            )
            view.accept(record, {"saved": True, "state": "revision"})
            return
        merge = _line_value(text, "merge") or ""
        tree = _line_value(text, "tree") or ""
        if not _COMMIT.fullmatch(merge) or not _COMMIT.fullmatch(tree):
            view.reject(record, "successful merge requires exact merge and tree object IDs")
            return
        task.update(
            state="complete",
            worker_id=None,
            merge_worker_id=None,
            merged_sha=merge,
            last_error=None,
        )
        view.frontier_sha = merge
        if all(item["state"] == "complete" for item in view.tasks.values()):
            view.state = "publishing"
        view.accept(
            record,
            {
                "saved": True,
                "state": "complete",
                "run_state": view.state,
            },
        )

    def _base_work(self, task: dict[str, Any], state: str, role: str, claim: str) -> dict[str, Any]:
        return {
            "kind": "work",
            "task_id": task["task_id"],
            "root_task_id": task["task_id"],
            "title": task["title"],
            "author_id": task["author_id"],
            "prompt": task["prompt"],
            "depends_on": task["depends_on"],
            "checks": task["checks"],
            "branch": task["branch"],
            "base_sha": task["base_sha"],
            "head_sha": task["head_sha"],
            "generation": task["generation"],
            "role": role,
            "state": state,
            "claim": claim,
            "worker_id": None,
            "commit_sha": task["merged_sha"] or task["head_sha"],
            "last_error": task["last_error"],
        }

    def _with_progress(
        self, view: Projection, task: dict[str, Any], value: dict[str, Any]
    ) -> dict[str, Any]:
        owner = value.get("worker_id")
        events = {value["claim"]}
        events.add(f"work-log: {value['claim'].removeprefix('claim: ')}")
        if value["role"] in {"author", "revision"}:
            events.update(
                {
                    f"checkpoint: task:{task['task_id']}",
                    f"handoff: task:{task['task_id']}",
                }
            )
        latest = next(
            (
                record
                for record in reversed(view.records)
                if record["author"] == owner
                and str(record["text"]).splitlines()[0] in events
                and view.outcomes.get(int(record["record_id"]), (False, ""))[0]
            ),
            None,
        )
        value.update(
            checkpoint=bool(task["checkpoint"]),
            handoff=bool(task["handoff"]),
            approvals=sum(item["state"] == "approved" for item in task["reviews"]),
            required_reviews=view.required_reviews,
            last_journal_record_id=(int(latest["record_id"]) if latest else None),
            last_journal_body=(_record_text(latest) if latest else None),
        )
        return value

    @staticmethod
    def _body(value: dict[str, Any]) -> dict[str, Any]:
        value["body"] = "\n".join(
            (
                "queue:work",
                f"task:{value['root_task_id']}",
                f"role:{value['role']}",
                f"state:{value['state']}",
                f"claim:{value['claim']}",
                f"title:{value['title']}",
                f"objective:{value['prompt']}",
                f"depends-on:{_compact(value['depends_on'])}",
                f"checks:{_compact(value['checks'])}",
                f"branch:{value['branch']}",
                f"base:{value['base_sha'] or ''}",
                f"head:{value['head_sha'] or ''}",
                f"generation:{value['generation']}",
                f"worker:{value['worker_id'] or ''}",
            )
        )
        return value

    def _author_work(self, view: Projection, task: dict[str, Any]) -> dict[str, Any]:
        state = (
            "working"
            if task["state"] == "authoring"
            else "ready"
            if task["state"] == "revision" or self._dependencies_complete(view, task)
            else "blocked"
        )
        role = "revision" if task["generation"] else "author"
        value = self._base_work(task, state, role, f"claim: task:{task['task_id']}")
        value["worker_id"] = task["worker_id"]
        return self._body(self._with_progress(view, task, value))

    def _review_work(
        self, view: Projection, task: dict[str, Any], review: dict[str, Any]
    ) -> dict[str, Any]:
        state = "working" if review["state"] == "claimed" else "ready"
        claim = f"claim: review:{task['task_id']}:{review['generation']}:{review['ordinal']}"
        value = self._base_work(task, state, f"review:{review['role']}", claim)
        value.update(
            task_id=f"{task['task_id']}/review-{review['ordinal']}",
            worker_id=review["worker_id"],
            review_ordinal=review["ordinal"],
            review_role=review["role"],
        )
        return self._body(self._with_progress(view, task, value))

    def _merge_work(self, view: Projection, task: dict[str, Any]) -> dict[str, Any]:
        state = "working" if task["state"] == "merge_claimed" else "ready"
        claim = f"claim: merge:{task['task_id']}:{task['generation']}"
        value = self._base_work(task, state, "merge", claim)
        value.update(task_id=f"{task['task_id']}/merge", worker_id=task["merge_worker_id"])
        return self._body(self._with_progress(view, task, value))

    def _verification_work(
        self, view: Projection, task: dict[str, Any], verification: dict[str, Any]
    ) -> dict[str, Any]:
        frontier = str(verification["frontier"])
        ordinal = int(verification["check_ordinal"])
        state = "working" if verification["state"] == "claimed" else "ready"
        claim = f"claim: verify:{task['task_id']}:{frontier}:{ordinal}"
        value = self._base_work(task, state, "verification", claim)
        value.update(
            task_id=f"{task['task_id']}/verify-{ordinal}",
            title=f"Independent verification: {task['title']}",
            prompt="Run the assigned acceptance command read-only against the exact assigned "
            "commit and journal the result.",
            checks=[task["checks"][ordinal - 1]],
            base_sha=frontier,
            head_sha=frontier,
            commit_sha=frontier,
            worker_id=verification["worker_id"],
            verification_ordinal=ordinal,
            verification_frontier=frontier,
        )
        return self._body(self._with_progress(view, task, value))

    def _verification_values(self, view: Projection) -> list[dict[str, Any]]:
        if view.state != "running" or not view.worker_capacity:
            return []
        values: list[dict[str, Any]] = []
        pending: list[dict[str, Any]] = []
        current_keys: set[str] = set()
        for task in sorted(view.tasks.values(), key=lambda item: item["ordinal"]):
            frontier = self._verification_head(view, task)
            if frontier is None:
                continue
            for ordinal in range(1, len(task["checks"]) + 1):
                key = self._verification_key(task["task_id"], frontier, ordinal)
                current_keys.add(key)
                verification = view.verifications.get(key)
                if verification is None:
                    verification = {
                        "task_id": task["task_id"],
                        "frontier": frontier,
                        "check_ordinal": ordinal,
                        "state": "pending",
                        "worker_id": None,
                        "result": None,
                        "detail": None,
                    }
                if verification["state"] == "claimed":
                    values.append(self._verification_work(view, task, verification))
                elif verification["state"] == "pending":
                    pending.append(self._verification_work(view, task, verification))
        for key, verification in view.verifications.items():
            if key in current_keys or verification["state"] != "claimed":
                continue
            task = view.tasks.get(str(verification["task_id"]))
            if task is not None:
                values.append(self._verification_work(view, task, verification))
        available = max(0, view.worker_capacity - len(values))
        values.extend(pending[:available])
        return values

    def _summary(self, view: Projection, task: dict[str, Any]) -> dict[str, Any]:
        approvals = sum(item["state"] == "approved" for item in task["reviews"])
        value = self._base_work(task, task["state"], "task", "")
        value.update(
            kind="task",
            author_id=task["author_id"],
            merge_worker_id=task["merge_worker_id"],
            merged_sha=task["merged_sha"],
            approvals=approvals,
            required_reviews=view.required_reviews,
            reviews=[dict(item) for item in task["reviews"]],
            verifications=[
                dict(item)
                for item in view.verifications.values()
                if item["task_id"] == task["task_id"]
                and item["frontier"] == self._verification_head(view, task)
            ],
        )
        return self._body(value)

    def work_values(self, view: Projection) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        for task in sorted(view.tasks.values(), key=lambda item: item["ordinal"]):
            if task["state"] in {"pending", "revision", "authoring"}:
                values.append(self._author_work(view, task))
            elif task["state"] == "reviewing":
                values.extend(
                    self._review_work(view, task, review)
                    for review in task["reviews"]
                    if review["state"] in {"pending", "claimed"}
                )
            elif task["state"] in {"merge_ready", "merge_claimed"}:
                values.append(self._merge_work(view, task))
            else:
                values.append(self._summary(view, task))
        values.extend(self._verification_values(view))
        return values

    def search(self, view: Projection, query: str) -> list[dict[str, Any]]:
        if view.stage is None:
            raise WorkflowError("workflow is not bootstrapped")
        if query == "run:state":
            return [{"kind": "run", "state": view.state, "body": f"state:{view.state}"}]
        if query in {"queue:all", "queue:ready"} or query.startswith("worker:"):
            values = self.work_values(view)
            if query == "queue:ready":
                return [value for value in values if value["state"] == "ready"]
            if query.startswith("worker:"):
                worker = query.removeprefix("worker:")
                return [
                    value
                    for value in values
                    if value["state"] == "working" and value["worker_id"] == worker
                ]
            return values
        if query == "merge:requested":
            return [
                self._summary(view, task)
                for task in sorted(view.tasks.values(), key=lambda item: item["ordinal"])
                if task["state"] == "merge_requested"
            ]
        if query.startswith("task:"):
            task = view.tasks.get(query.removeprefix("task:"))
            return [self._summary(view, task)] if task is not None else []
        raise WorkflowError("query is not a workflow projection")


class WorkflowClient:
    def __init__(
        self,
        journal: JournalPort,
        workload: dict[str, Any] | None = None,
        workload_ref: dict[str, Any] | None = None,
        *,
        replay_root: str | None = None,
        epoch: int = 0,
        history_sequence: int = 0,
        epoch_frontiers: list[dict[str, int]] | None = None,
        serialization_path: str | Path | None = None,
    ) -> None:
        if replay_root is not None and re.fullmatch(r"[0-9a-f]{32}", replay_root) is None:
            raise WorkflowError("replay root must be 128-bit lowercase hexadecimal")
        if epoch < 0 or history_sequence < 0:
            raise WorkflowError("epoch and history sequence must be nonnegative")
        self.journal = journal
        self._workload = workload
        self.workload_ref = workload_ref or (
            {
                "schema": "SwarmWorkloadRefV1",
                "workload_blob": hashlib.sha256(_compact(workload).encode()).hexdigest(),
            }
            if workload is not None
            else None
        )
        self.replay_root = replay_root
        self.epoch = epoch
        self.history_sequence = history_sequence
        self.reducer = WorkflowReducer(
            workload,
            self.workload_ref,
            epoch=epoch,
            history_sequence=history_sequence,
            epoch_frontiers=epoch_frontiers,
        )
        self._views: dict[str, Projection] = {}
        inferred_socket = getattr(journal, "socket_path", None)
        self.serialization_path = (
            Path(serialization_path)
            if serialization_path
            else (Path(str(inferred_socket) + ".workflow.lock") if inferred_socket else None)
        )

    def _root(self, namespace: str) -> str:
        return (
            self.replay_root
            or hashlib.sha256(f"private-workflow-replay:{namespace}".encode()).hexdigest()[:32]
        )

    @contextmanager
    def _serialized(self):
        if self.serialization_path is None:
            yield None
            return
        self.serialization_path.parent.mkdir(parents=True, exist_ok=True)
        with self.serialization_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            lock.seek(0)
            try:
                state = json.loads(lock.read() or "{}")
            except json.JSONDecodeError:
                state = {}
            yield state
            lock.seek(0)
            lock.truncate()
            lock.write(_compact(state))
            lock.flush()
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _partition_records(
        self, namespace: str, prefix: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        root = self._root(namespace)
        query = f"swarm-routing:{root}:{prefix}"
        returned = self.journal.search(namespace, query)
        exact: list[dict[str, Any]] = []
        for record in returned:
            route = _record_route(record)
            if (
                route is not None
                and record.get("namespace") == namespace
                and route["run_id"] == namespace
                and route["replay_root"] == root
                and str(route["replay_id"]).startswith(prefix)
                and query in str(record.get("text", ""))
            ):
                exact.append(record)
        return exact, returned

    def _replay_records(self, namespace: str, prefix: str = "") -> list[dict[str, Any]]:
        pending = [prefix]
        recovered: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            while pending:
                prefixes = pending
                pending = []
                batches = pool.map(
                    lambda value: self._partition_records(namespace, value)[0],
                    prefixes,
                )
                for value, records in zip(prefixes, batches, strict=True):
                    if not records:
                        # Semantic extras do not prove that this exact replay partition exists.
                        continue
                    for record in records:
                        route = _record_route(record)
                        assert route is not None
                        stable = str(route["stable_id"])
                        prior = recovered.get(stable)
                        if prior is not None and int(prior["journal_sequence"]) != int(
                            record["journal_sequence"]
                        ):
                            raise WorkflowError("stable journal identity was reused")
                        recovered[stable] = record
                    if len(value) < _REPLAY_WIDTH:
                        pending.extend(value + suffix for suffix in _REPLAY_FANOUT)
                    else:
                        leaf_ids = {
                            str((_record_route(record) or {}).get("stable_id"))
                            for record in records
                        }
                        if len(leaf_ids) != 1:
                            raise WorkflowError("replay leaf identity is not unique")
        return list(recovered.values())

    def _records(self, namespace: str) -> list[dict[str, Any]]:
        replayed = self._replay_records(namespace)
        ordered: dict[int, dict[str, Any]] = {}
        for record in replayed:
            try:
                sequence = int(record["journal_sequence"])
            except (KeyError, TypeError, ValueError) as error:
                raise WorkflowError("journal record is missing its stable sequence") from error
            if sequence in ordered and _record_route(ordered[sequence]) != _record_route(record):
                raise WorkflowError("journal sequence identifies multiple records")
            ordered[sequence] = record
        return [ordered[key] for key in sorted(ordered)]

    def _latest_sequence(self, namespace: str) -> int:
        """Return the newest workflow sequence through the bounded SEARCH contract.

        Every workflow record exactly matches the private replay root.  The exact
        floor therefore guarantees at least one exact root anchor whenever the
        run is nonempty, and recent-first bounded selection guarantees that one of
        those anchors is the newest workflow record.  Semantic extras are ignored.
        """

        root = self._root(namespace)
        exact, _ = self._partition_records(namespace, "")
        sequences = [
            int(record["journal_sequence"])
            for record in exact
            if (_record_route(record) or {}).get("replay_root") == root
        ]
        return max(sequences, default=0)

    def replay_records(self, namespace: str) -> list[dict[str, Any]]:
        """Return the complete ordered workflow log through journal_search only."""

        return self._records(namespace)

    def workload(self, namespace: str) -> dict[str, Any]:
        """Return the repository-loaded workload bound by the bootstrap reference."""

        view = self._view(namespace)
        if view.stage_bytes is None:
            raise WorkflowError("workflow is not bootstrapped")
        value = json.loads(view.stage_bytes)
        assert isinstance(value, dict)
        return value

    def _view(self, namespace: str) -> Projection:
        cached = self._views.get(namespace)
        if cached is not None:
            cached_sequence = int(cached.records[-1]["journal_sequence"]) if cached.records else 0
            if self._latest_sequence(namespace) == cached_sequence:
                return cached
        records = self._records(namespace)
        view = cached
        if (
            view is None
            or len(records) < len(view.records)
            or (
                view.records
                and records[len(view.records) - 1]["record_id"] != view.records[-1]["record_id"]
            )
        ):
            view = self.reducer.reduce(namespace, records)
        else:
            for record in records[len(view.records) :]:
                view.records.append(record)
                self.reducer._apply(view, record)
        self._views[namespace] = view
        return view

    def bootstrap(self, namespace: str) -> dict[str, Any]:
        if self._workload is None or self.workload_ref is None:
            raise WorkflowError("bootstrap requires a frozen repository workload")
        return self.add(
            namespace,
            "launcher",
            "\n".join(
                (
                    f"bootstrap: run:{namespace}",
                    f"workload-ref: {_compact(self.workload_ref)}",
                )
            ),
        )

    @staticmethod
    def _scheduled_ready(view: Projection, values: list[dict[str, Any]]) -> dict[str, list]:
        scheduled: dict[str, list] = {}
        free_workers = {f"worker-{ordinal}" for ordinal in range(view.worker_capacity)} - {
            str(value["worker_id"])
            for value in values
            if value["state"] == "working" and value.get("worker_id")
        }
        for value in (item for item in values if item["state"] == "ready"):
            role = str(value.get("role", ""))
            family = (
                "verification"
                if role == "verification"
                else "review"
                if role.startswith("review:")
                else role
            )
            eligible = sorted(free_workers)
            if not eligible:
                continue
            identity = f"{value.get('root_task_id')}:{value.get('generation')}:{family}"
            digest = hashlib.sha256(identity.encode("utf-8")).digest()
            offset = int.from_bytes(digest[:8], "big")
            ordinal = int(value.get("verification_ordinal") or value.get("review_ordinal") or 1)
            worker = eligible[(offset + ordinal - 1) % len(eligible)]
            scheduled.setdefault(worker, []).append(value)
            free_workers.remove(worker)
        return scheduled

    def worker_snapshot(self, namespace: str, worker: str) -> tuple[str, list, list]:
        view = self._view(namespace)
        values = self.reducer.work_values(view)
        active = [
            value
            for value in values
            if value["state"] == "working" and value["worker_id"] == worker
        ]
        ready = self._scheduled_ready(view, values).get(worker, [])
        return view.state, active, ready

    def add(self, namespace: str, author: str, text: str) -> dict[str, Any]:
        first, separator, remainder = text.partition("\n")
        for prefix in ("work-log: ", "verify-pass: ", "verify-fail: "):
            doubled = prefix + "claim: "
            if first.startswith(doubled):
                first = prefix + first.removeprefix(doubled)
                text = first + (separator + remainder if separator else "")
                break
        root = self._root(namespace)
        with self._serialized() as lock_state:
            if (
                isinstance(lock_state, dict)
                and int(lock_state.get("epoch", self.epoch)) > self.epoch
            ):
                raise WorkflowError("client run epoch is stale")
            # Synchronize stable history while ownership is serialized, then append and
            # recover the new record through its unique exact routing identity.
            view = (
                self._view(namespace)
                if self._views.get(namespace) is None
                else self._views[namespace]
            )
            if isinstance(lock_state, dict):
                cached_sequence = int(view.records[-1]["journal_sequence"]) if view.records else 0
                if int(lock_state.get("sequence", cached_sequence)) != cached_sequence:
                    view = self.reducer.reduce(namespace, self._records(namespace))
                    self._views[namespace] = view
            stable_id = secrets.token_hex(16)
            # Dense monotonically allocated leaves preserve fixed-width uniqueness
            # while keeping the deterministic partition tree compact. Allocation is
            # protected by the same run lock that serializes authoritative replay.
            replay_ordinal = len(view.records)
            for _ in range(16):
                if replay_ordinal >= 2**_REPLAY_WIDTH:
                    raise WorkflowError("workflow replay identity space is exhausted")
                replay_id = f"{replay_ordinal:0{_REPLAY_WIDTH}b}"
                if not self._partition_records(namespace, replay_id)[0]:
                    break
                replay_ordinal += 1
            else:
                raise WorkflowError("could not allocate a unique replay leaf")
            routed = "\n".join(
                (
                    text.rstrip(),
                    f"swarm-run:{namespace}",
                    f"swarm-epoch:{self.epoch}",
                    f"swarm-stable:{stable_id}",
                    f"swarm-routing:{root}:{replay_id}",
                )
            )
            stored = self.journal.add(namespace, author, routed)
            record_id = int(stored["record_id"])
            exact = [
                record
                for record in self.journal.search(namespace, f"swarm-stable:{stable_id}")
                if (_record_route(record) or {}).get("stable_id") == stable_id
            ]
            if len(exact) != 1:
                raise WorkflowError("new journal record was not recovered by stable identity")
            record = exact[0]
            view.records.append(record)
            self.reducer._apply(view, record)
            if isinstance(lock_state, dict):
                lock_state.update(epoch=self.epoch, sequence=int(record["journal_sequence"]))
        outcome = view.outcomes.get(record_id)
        if outcome is None:
            raise WorkflowError("new journal record was not replayed")
        accepted, value = outcome
        if not accepted:
            raise WorkflowError(str(value))
        assert isinstance(value, dict)
        return {**value, "record_id": record_id}

    def search(self, namespace: str, query: str) -> list[dict[str, Any]]:
        if not query or len(query.encode("utf-8")) > 4096:
            raise WorkflowError("query must be 1..4096 UTF-8 bytes")
        projection_query = query in {
            "run:state",
            "queue:all",
            "queue:ready",
            "merge:requested",
        } or query.startswith(("worker:", "task:"))
        if not projection_query:
            records = self.journal.search(namespace, query)
            return [
                {
                    "kind": "entry",
                    "record_id": record["record_id"],
                    "journal_sequence": record["journal_sequence"],
                    "stable_id": (_record_route(record) or {}).get("stable_id"),
                    "run_id": (_record_route(record) or {}).get("run_id"),
                    "epoch": (_record_route(record) or {}).get("epoch"),
                    "routing_key": (
                        f"{(_record_route(record) or {}).get('replay_root')}:"
                        f"{(_record_route(record) or {}).get('replay_id')}"
                    ),
                    "match_kind": record.get("match_kind"),
                    "worker_id": record["author"],
                    "body": _record_text(record),
                    "created_at": record["created_at"],
                }
                for record in records
            ]
        view = self._view(namespace)
        return self.reducer.search(view, query)
