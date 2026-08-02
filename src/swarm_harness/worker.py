"""Real Codex worker adapter with Git isolation and process-group cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .journal_client import JournalClient
from .tools import claim_task, submit_result


class WorkerError(RuntimeError):
    pass


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and completed.returncode:
        raise WorkerError(completed.stderr.strip() or "Git command failed")
    return completed.stdout.strip()


class Worker:
    def __init__(
        self,
        client: JournalClient,
        run_id: str,
        worker_id: str,
        *,
        lease_seconds: float | None = None,
        codex_argv: Sequence[str] = ("codex", "exec"),
        model: str | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.codex_argv = list(codex_argv)
        self.model = model
        self.log = log

    @staticmethod
    def _prompt(envelope: dict) -> str:
        checks = "\n".join(f"- {item}" for item in envelope["acceptance_checks"])
        return (
            "Implement exactly this assigned task in the current Git worktree.\n\n"
            f"Task: {envelope['task_id']} — {envelope['title']}\n"
            f"Objective: {envelope['prompt']}\n\n"
            "Acceptance checks:\n"
            f"{checks or '- No additional command.'}\n\n"
            "Parallel ownership rule: other workers are implementing sibling tasks from the "
            "same base. Treat root Cargo.toml, Cargo.lock, tools/verifier_support.rs, and "
            "pre-seeded package manifests as shared read-only files. Prefer new files whose "
            "names are specific to this task, and do not edit sibling task outputs. Only "
            "change a shared file when this task explicitly requires that exact file.\n\n"
            "Work only in this repository. Run the relevant checks. Do not create follow-up "
            "tasks. Finish with a concise summary; the worker adapter owns submission and will "
            "commit any remaining worktree changes."
        )

    def _run_codex(self, worktree: Path, envelope: dict) -> tuple[int, str]:
        argv = [
            *self.codex_argv,
            "-C",
            str(worktree),
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "--json",
        ]
        if self.model:
            argv += ["--model", self.model]
        argv.append(self._prompt(envelope))
        process = subprocess.Popen(
            argv,
            cwd=worktree,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        output: list[str] = []
        raw_deadline = envelope.get("lease_expires_at")
        deadline = None if raw_deadline is None else float(raw_deadline)
        assert process.stdout is not None
        try:
            while process.poll() is None:
                line = process.stdout.readline()
                if line:
                    line = line.rstrip("\n")
                    output.append(line)
                    self.log(line)
                if deadline is not None and time.time() >= deadline:
                    raise TimeoutError("worker lease deadline reached")
            for line in process.stdout:
                line = line.rstrip("\n")
                output.append(line)
                self.log(line)
            return int(process.returncode or 0), "\n".join(output[-20:])
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise

    @staticmethod
    def _commit(worktree: Path, task_id: str) -> str:
        if _git(worktree, "status", "--porcelain=v1", "--untracked-files=all"):
            if not _git(worktree, "config", "user.name", check=False):
                _git(worktree, "config", "user.name", "Swarm Worker")
            if not _git(worktree, "config", "user.email", check=False):
                _git(worktree, "config", "user.email", "swarm-worker@localhost")
            _git(worktree, "add", "-A")
            _git(worktree, "commit", "-m", f"Complete {task_id}")
        return _git(worktree, "rev-parse", "HEAD")

    def run_once(self) -> str:
        envelope = claim_task(
            self.client,
            run_id=self.run_id,
            worker_id=self.worker_id,
            lease_seconds=self.lease_seconds,
        )
        status = str(envelope["status"])
        if status != "claimed":
            return status
        task_id = str(envelope["task_id"])
        token = str(envelope["claim_token"])
        worktree = Path(envelope["worktree_path"])
        self.log(f"claimed {task_id} in {worktree}")
        outcome = "failed"
        summary = "worker failed before completion"
        commit = _git(worktree, "rev-parse", "HEAD")
        blockers: list[object] = []
        try:
            returncode, output = self._run_codex(worktree, envelope)
            commit = self._commit(worktree, task_id)
            if returncode == 0:
                outcome = "completed"
                summary = output or f"Completed {task_id}"
            else:
                summary = f"Codex exited {returncode}\n{output}".strip()
                blockers = [{"kind": "codex_exit", "returncode": returncode}]
        except (OSError, subprocess.SubprocessError, TimeoutError, WorkerError) as error:
            summary = f"{type(error).__name__}: {error}"
            blockers = [{"kind": "worker_error", "message": str(error)}]
        submit_result(
            self.client,
            run_id=self.run_id,
            task_id=task_id,
            claim_token=token,
            outcome=outcome,
            summary=summary,
            commit_sha=commit,
            blockers=blockers,
        )
        self.log(f"submitted {task_id}: {outcome} {commit[:12]}")
        return "submitted"

    def run(self, idle_seconds: float = 1.0) -> str:
        while True:
            state = self.run_once()
            if state in {"complete", "paused", "failed", "stopped"}:
                self.log(f"worker {self.worker_id}: run {state}")
                return state
            if state == "idle":
                status = self.client.run_status(self.run_id)["run"]["state"]
                if status in {"complete", "failed", "stopped"}:
                    return status
                time.sleep(idle_seconds)
