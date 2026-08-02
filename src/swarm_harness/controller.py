"""Dependency scheduler, acceptance verifier, and Git integration loop."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .journal_client import JournalClient


class ControllerError(RuntimeError):
    pass


def _scalar(raw: str) -> Any:
    value = raw.strip()
    if value == "[]":
        return []
    if value in {"true", "false"}:
        return value == "true"
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def load_stage(path: str | Path) -> dict[str, Any]:
    """Parse the deliberately small, closed stage-manifest YAML subset."""

    result: dict[str, Any] = {"tasks": [], "stage_gate": []}
    section: str | None = None
    task: dict[str, Any] | None = None
    task_list: str | None = None
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = raw.strip()
        if indent == 0:
            task = None
            task_list = None
            if line in {"tasks:", "stage_gate:"}:
                section = line[:-1]
                continue
            if ":" not in line:
                raise ControllerError(f"invalid stage manifest line {number}")
            key, value = line.split(":", 1)
            result[key] = _scalar(value)
            section = None
            continue
        if section == "tasks":
            if indent == 2 and line.startswith("- id:"):
                task = {
                    "id": str(_scalar(line.split(":", 1)[1])),
                    "depends_on": [],
                    "checks": [],
                }
                result["tasks"].append(task)
                task_list = None
                continue
            if task is None:
                raise ControllerError(f"task field precedes task at line {number}")
            if indent == 4 and ":" in line:
                key, value = line.split(":", 1)
                parsed = _scalar(value)
                if key in {"depends_on", "checks"}:
                    task_list = key
                    task[key] = (
                        parsed
                        if isinstance(parsed, list)
                        else ([] if not str(parsed) else [parsed])
                    )
                else:
                    task[key] = parsed
                    task_list = None
                continue
            if indent == 6 and line.startswith("- ") and task_list:
                task[task_list].append(str(_scalar(line[2:])))
                continue
        if section == "stage_gate" and indent == 2 and line.startswith("- "):
            result["stage_gate"].append(str(_scalar(line[2:])))
            continue
        raise ControllerError(f"unsupported stage manifest line {number}: {line}")
    required = {"stage", "enabled", "goal", "tasks", "stage_gate"}
    if not required.issubset(result):
        raise ControllerError(f"stage manifest is missing {sorted(required - set(result))}")
    if result["enabled"] is not True:
        raise ControllerError(f"stage {result['stage']} is disabled")
    if not result["tasks"]:
        raise ControllerError("stage has no human-approved tasks")
    for item in result["tasks"]:
        if not {"id", "title", "prompt", "depends_on", "checks"}.issubset(item):
            raise ControllerError(f"task is incomplete: {item.get('id', '<unknown>')}")
    return result


def _git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode:
        raise ControllerError(completed.stderr.strip() or "Git command failed")
    return completed


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


class Controller:
    def __init__(
        self,
        client: JournalClient,
        run_id: str,
        workload: str,
        stage: dict[str, Any],
        target_repo: str | Path,
        run_dir: str | Path,
        log: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.workload = workload
        self.stage = stage
        self.target_repo = Path(target_repo).resolve()
        self.run_dir = Path(run_dir).resolve()
        self.integration = self.run_dir / "integration"
        self.integration_branch = f"codex/swarm-{_slug(run_id)}/integration"
        self.log = log

    def seed(self) -> dict:
        if _git(self.target_repo, "rev-parse", "--is-inside-work-tree").stdout.strip() != "true":
            raise ControllerError("target must be a Git worktree")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if not self.integration.exists():
            existing = _git(
                self.target_repo,
                "show-ref",
                "--verify",
                f"refs/heads/{self.integration_branch}",
                check=False,
            ).returncode == 0
            arguments = ["worktree", "add"]
            if not existing:
                arguments += ["-b", self.integration_branch]
            arguments += [str(self.integration), self.integration_branch if existing else "HEAD"]
            _git(self.target_repo, *arguments)
        result = self.client.call(
            "create_run",
            run_id=self.run_id,
            workload=self.workload,
            target_repo=str(self.target_repo),
            integration_branch=self.integration_branch,
            integration_worktree=str(self.integration),
            tasks=self.stage["tasks"],
        )
        self.log(f"run {self.run_id}: journal {'created' if result['created'] else 'resumed'}")
        return result

    def prepare_runnable(self) -> int:
        rows = self.client.call("runnable_unprepared", run_id=self.run_id)
        prepared = 0
        for row in rows:
            task_id = row["task_id"]
            nonce = hashlib.sha256(f"{task_id}:{time.time_ns()}".encode()).hexdigest()[:8]
            branch = f"codex/swarm-{_slug(self.run_id)}/{_slug(task_id)}-{nonce}"
            worktree = self.run_dir / "worktrees" / f"{_slug(task_id)}-{nonce}"
            worktree.parent.mkdir(parents=True, exist_ok=True)
            _git(self.target_repo, "worktree", "add", "-b", branch, str(worktree), self.integration_branch)
            self.client.call(
                "prepare_task",
                run_id=self.run_id,
                task_id=task_id,
                branch=branch,
                worktree_path=str(worktree),
            )
            self.log(f"prepared {task_id} on {branch}")
            prepared += 1
        return prepared

    @staticmethod
    def _run_checks(worktree: Path, checks: list[str]) -> tuple[bool, str]:
        for command in checks:
            completed = subprocess.run(
                command,
                cwd=worktree,
                shell=True,
                executable="/bin/sh",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
            if completed.returncode:
                return False, f"{command}\n{completed.stdout}".strip()
        return True, ""

    def adjudicate(self) -> int:
        rows = self.client.call("submitted_tasks", run_id=self.run_id)
        handled = 0
        for row in rows:
            task_id = row["task_id"]
            submission = row["submission"]
            worktree = Path(row["worktree_path"])
            commit = submission.get("commit_sha", "")
            error = ""
            if submission.get("outcome") != "completed":
                error = submission.get("summary") or "worker reported failure"
            elif not re.fullmatch(r"[0-9a-f]{40}", commit):
                error = "worker did not submit an exact Git commit"
            elif _git(worktree, "cat-file", "-e", f"{commit}^{{commit}}", check=False).returncode:
                error = "submitted commit is absent from the task worktree"
            elif _git(worktree, "status", "--porcelain=v1", "--untracked-files=all").stdout:
                error = "task worktree is dirty after submission"
            else:
                _passed, error = self._run_checks(worktree, json.loads(row["checks_json"]))
            if error:
                self.client.call("reject_task", run_id=self.run_id, task_id=task_id, error=error)
                self.log(f"rejected {task_id}: {error.splitlines()[0]}")
                handled += 1
                continue
            merged = _git(self.integration, "merge", "--no-ff", "--no-edit", commit, check=False)
            if merged.returncode:
                _git(self.integration, "merge", "--abort", check=False)
                error = merged.stderr.strip() or "integration merge failed"
                self.client.call("reject_task", run_id=self.run_id, task_id=task_id, error=error)
                self.log(f"rejected {task_id}: integration conflict")
            else:
                integrated = _git(self.integration, "rev-parse", "HEAD").stdout.strip()
                self.client.call(
                    "accept_task",
                    run_id=self.run_id,
                    task_id=task_id,
                    commit_sha=integrated,
                )
                self.log(f"accepted {task_id} at {integrated[:12]}")
            handled += 1
        return handled

    def tick(self) -> str:
        current = self.client.run_status(self.run_id)["run"]["state"]
        if current != "running":
            return str(current)
        self.adjudicate()
        self.prepare_runnable()
        status = self.client.run_status(self.run_id)
        states = [task["state"] for task in status["tasks"]]
        if states and all(state == "accepted" for state in states):
            passed, error = self._run_checks(self.integration, list(self.stage["stage_gate"]))
            if passed:
                self.client.call("set_run_state", run_id=self.run_id, state="complete")
                self.log(f"run {self.run_id}: COMPLETE")
                return "complete"
            self.client.call("set_run_state", run_id=self.run_id, state="failed")
            self.log(f"run {self.run_id}: stage gate failed: {error}")
            return "failed"
        return status["run"]["state"]

    def run(self, poll_seconds: float = 0.5) -> str:
        self.seed()
        while True:
            state = self.tick()
            if state in {"complete", "paused", "failed", "stopped"}:
                return state
            time.sleep(poll_seconds)
