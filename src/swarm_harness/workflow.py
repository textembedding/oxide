"""Replayable swarm workflow interpreted entirely above the generic journal."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .journal import JournalClient


class WorkflowError(RuntimeError):
    pass


_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_AUTHOR_CLAIM = re.compile(r"^claim: task:([^\s:]+)$")
_REVIEW_CLAIM = re.compile(r"^claim: review:([^\s:]+):(\d+):(\d+)$")
_MERGE_CLAIM = re.compile(r"^claim: merge:([^\s:]+):(\d+)$")
_AUTHOR_MARKER = re.compile(r"^(checkpoint|handoff|open-pr): task:([^\s:]+)$")
_REVIEW_DECISION = re.compile(r"^(approve|challenge): review:([^\s:]+):(\d+):(\d+)$")
_MERGE_REQUEST = re.compile(r"^merge: task:([^\s:]+)$")
_REVIEW_ROLES = ("specification", "adversarial", "integration")


def _compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _line_value(text: str, name: str) -> str | None:
    prefix = name + ":"
    for line in text.splitlines()[1:]:
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()


@dataclass
class Projection:
    namespace: str
    records: list[dict[str, Any]]
    state: str = "uninitialized"
    stage: dict[str, Any] | None = None
    stage_bytes: str | None = None
    required_reviews: int = 0
    tasks: dict[str, dict[str, Any]] = field(default_factory=dict)
    outcomes: dict[int, tuple[bool, dict[str, Any] | str]] = field(default_factory=dict)

    def accept(self, record: dict[str, Any], value: dict[str, Any]) -> None:
        self.outcomes[int(record["record_id"])] = (True, value)

    def reject(self, record: dict[str, Any], reason: str) -> None:
        self.outcomes[int(record["record_id"])] = (False, reason)


class WorkflowReducer:
    """Pure ordered-log reduction; it never writes to the journal."""

    def reduce(self, namespace: str, records: list[dict[str, Any]]) -> Projection:
        view = Projection(namespace=namespace, records=records)
        for record in records:
            self._apply(view, record)
        return view

    def _apply(self, view: Projection, record: dict[str, Any]) -> None:
        text = str(record["text"])
        author = str(record["author"])
        first = text.splitlines()[0]
        if first.startswith("bootstrap:"):
            self._bootstrap(view, record)
            return
        if view.stage is None:
            view.reject(record, "workflow is not bootstrapped")
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
        else:
            view.accept(record, {"saved": True, "record_id": record["record_id"]})

    def _bootstrap(self, view: Projection, record: dict[str, Any]) -> None:
        text = str(record["text"])
        expected = f"bootstrap: run:{view.namespace}"
        raw = _line_value(text, "stage-json")
        if record["author"] != "launcher" or text.splitlines()[0] != expected or raw is None:
            view.reject(record, "only the launcher may bootstrap its exact workflow")
            return
        try:
            stage = json.loads(raw)
        except json.JSONDecodeError:
            view.reject(record, "stage-json is invalid")
            return
        canonical = _compact(stage)
        if view.stage is not None:
            if canonical != view.stage_bytes:
                view.reject(record, "workflow was bootstrapped with different stage bytes")
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
                }
            )
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
        return False

    def _claim_author(self, view: Projection, record: dict[str, Any], task_id: str) -> None:
        task = self._task(view, task_id)
        worker = str(record["author"])
        if task is None:
            view.reject(record, "unknown task")
        elif task["state"] == "authoring" and task["worker_id"] == worker:
            view.accept(
                record, {"saved": False, "claim": "resumed", "work": self._author_work(view, task)}
            )
        elif task["state"] not in {"pending", "revision"}:
            view.reject(record, f"task is {task['state']}")
        elif not self._dependencies_complete(view, task):
            view.reject(record, "task dependencies are not complete")
        elif self._owned(view, worker):
            view.reject(record, "worker already owns work")
        else:
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
            view.reject(record, f"review is {review['state']}")
        elif worker == task["author_id"]:
            view.reject(record, "an author cannot review its own candidate")
        elif self._owned(view, worker) or any(
            item["worker_id"] == worker and item["state"] in {"claimed", "approved", "challenged"}
            for item in task["reviews"]
            if item["generation"] == generation
        ):
            view.reject(record, "reviewers must be distinct and own one work item")
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

    def _open_pr(self, view: Projection, record: dict[str, Any], task: dict[str, Any]) -> None:
        text = str(record["text"])
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
        head = _line_value(str(record["text"]), "head") or ""
        detail = _line_value(str(record["text"]), "evidence" if decision == "approve" else "reason")
        if (
            task is None
            or review is None
            or task["state"] != "reviewing"
            or task["generation"] != generation
            or review["state"] != "claimed"
            or review["worker_id"] != worker
            or head != task["head_sha"]
            or head != review["head_sha"]
            or _line_value(str(record["text"]), "verified") != "true"
            or not detail
        ):
            view.reject(record, "review decision is not bound to the owned current candidate")
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
                    _line_value(str(record["text"]), "reason")
                    or "internal reviewer challenged the candidate"
                )[:2000],
            )
            view.accept(record, {"saved": True, "decision": "challenge"})
            return
        review["state"] = "approved"
        approvals = sum(item["state"] == "approved" for item in task["reviews"])
        if approvals == view.required_reviews:
            task["state"] = "merge_ready"
        view.accept(record, {"saved": True, "decision": "approve", "approvals": approvals})

    def _merge_request(self, view: Projection, record: dict[str, Any], task_id: str) -> None:
        task = self._task(view, task_id)
        text = str(record["text"])
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
        text = str(record["text"])
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
            last_journal_body=(str(latest["text"]) if latest else None),
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
        matches: list[dict[str, Any]] = []
        if query.startswith("task:"):
            task = view.tasks.get(query.removeprefix("task:"))
            if task is not None:
                matches.append(self._summary(view, task))
        matches.extend(
            {
                "kind": "entry",
                "record_id": record["record_id"],
                "worker_id": record["author"],
                "body": record["text"],
                "created_at": record["created_at"],
            }
            for record in reversed(view.records)
            if query in record["text"]
        )
        return matches[:100]


class WorkflowClient:
    """Two-call workflow facade backed only by generic journal records."""

    def __init__(self, journal: JournalClient) -> None:
        self.journal = journal
        self.socket_path = journal.socket_path
        self.reducer = WorkflowReducer()

    def _view(self, namespace: str) -> Projection:
        return self.reducer.reduce(namespace, self.journal.search(namespace, "*"))

    def add(self, namespace: str, author: str, text: str) -> dict[str, Any]:
        stored = self.journal.add(namespace, author, text)
        record_id = int(stored["record_id"])
        outcome = self._view(namespace).outcomes.get(record_id)
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
        view = self._view(namespace)
        return self.reducer.search(view, query)
