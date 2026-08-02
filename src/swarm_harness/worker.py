"""Real Codex worker adapter with Git isolation and process-group cleanup."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .journal_client import JournalClient


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
            "Journal protocol:\n"
            "- The only journal tools available to you are journal_search and journal_add. "
            "Never open the journal socket or database directly.\n"
            f"- Before inspecting or editing, call journal_search for `task:{envelope['task_id']}`.\n"
            f"- After the first durable edit, call journal_add with the exact marker "
            f"`checkpoint: task:{envelope['task_id']}` and summarize durable state. If this is "
            "a resumed worktree, checkpoint the recovered state immediately after inspection.\n"
            f"- Before finishing, call journal_add with the exact marker "
            f"`handoff: task:{envelope['task_id']}` plus completed work, files, checks, and risks.\n"
            "- Both tools take one `yaml` argument whose value is YAML containing exactly one "
            "string field: `query` for journal_search or `text` for journal_add.\n\n"
            "Parallel ownership rule: other workers are implementing sibling tasks from the "
            "same base. Treat root Cargo.toml, Cargo.lock, tools/verifier_support.rs, and "
            "pre-seeded package manifests as shared read-only files. Prefer new files whose "
            "names are specific to this task, and do not edit sibling task outputs. Only "
            "change a shared file when this task explicitly requires that exact file.\n\n"
            "Work only in this repository. Run the relevant checks. Do not create follow-up "
            "tasks. Finish with a concise summary; the worker adapter owns submission and will "
            "commit any remaining worktree changes."
        )

    def _mcp_configs(self) -> list[str]:
        forwarded = [
            "PYTHONPATH",
            "SWARM_CLAIM_TOKEN",
            "SWARM_JOURNAL_SOCKET",
            "SWARM_RUN_ID",
            "SWARM_TASK_ID",
            "SWARM_WORKER_ID",
        ]
        return [
            'approval_policy="never"',
            'web_search="disabled"',
            'shell_environment_policy.exclude=["SWARM_*"]',
            f"mcp_servers.journal.command={json.dumps(str(Path(sys.executable).resolve()))}",
            'mcp_servers.journal.args=["-m","swarm_harness.journal_mcp"]',
            f"mcp_servers.journal.env_vars={json.dumps(forwarded)}",
            "mcp_servers.journal.required=true",
            'mcp_servers.journal.enabled_tools=["journal_add","journal_search"]',
            "mcp_servers.journal.startup_timeout_sec=5",
            "mcp_servers.journal.tool_timeout_sec=10",
        ]

    def _run_codex(self, worktree: Path, envelope: dict) -> tuple[int, str]:
        argv = [
            *self.codex_argv,
            "--ignore-user-config",
            "--strict-config",
            "--ignore-rules",
            "-C",
            str(worktree),
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "--json",
        ]
        for config in self._mcp_configs():
            argv.extend(("-c", config))
        if self.model:
            argv += ["--model", self.model]
        argv.append(self._prompt(envelope))
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "SWARM_CLAIM_TOKEN": str(envelope["claim_token"]),
                "SWARM_JOURNAL_SOCKET": self.client.socket_path,
                "SWARM_RUN_ID": self.run_id,
                "SWARM_TASK_ID": str(envelope["task_id"]),
                "SWARM_WORKER_ID": self.worker_id,
            }
        )
        process = subprocess.Popen(
            argv,
            cwd=worktree,
            env=environment,
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

    @staticmethod
    def _validate(envelope: dict) -> tuple[str, dict]:
        """Independently validate one immutable proposal without model authority."""

        kind = str(envelope["proposal_kind"])
        payload = envelope.get("payload") or {}
        evidence: dict[str, object] = {
            "proposal_kind": kind,
            "proposal_id": int(envelope["proposal_id"]),
            "checks": [],
        }
        if kind in {"task_acceptance", "stage_completion"}:
            worktree = Path(envelope["worktree_path"])
            expected = str(
                payload.get("candidate_commit")
                if kind == "task_acceptance"
                else payload.get("integration_head")
            )
            try:
                observed = _git(worktree, "rev-parse", "HEAD")
                dirty = _git(
                    worktree,
                    "status",
                    "--porcelain=v1",
                    "--untracked-files=all",
                )
            except WorkerError as error:
                evidence["reason"] = str(error)
                return "reject", evidence
            evidence["expected_head"] = expected
            evidence["observed_head"] = observed
            evidence["clean"] = not dirty
            if not re.fullmatch(r"[0-9a-f]{40}", expected):
                evidence["reason"] = "proposal does not bind an exact Git commit"
                return "reject", evidence
            if observed != expected:
                evidence["reason"] = "proposal head differs from the validation worktree"
                return "reject", evidence
            if dirty:
                evidence["reason"] = "validation worktree is dirty"
                return "reject", evidence
            check_evidence: list[dict[str, object]] = []
            for command in envelope.get("acceptance_checks", []):
                completed = subprocess.run(
                    str(command),
                    cwd=worktree,
                    shell=True,
                    executable="/bin/sh",
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                )
                check_evidence.append(
                    {
                        "command": str(command),
                        "returncode": completed.returncode,
                        "output_tail": completed.stdout[-4000:],
                    }
                )
                if completed.returncode:
                    evidence["checks"] = check_evidence
                    evidence["reason"] = f"acceptance check failed: {command}"
                    return "reject", evidence
            evidence["checks"] = check_evidence
            evidence["reason"] = "exact head, clean tree, and all checks passed"
            return "approve", evidence
        if kind == "task_retry":
            submission = payload.get("submission")
            justified = bool(payload.get("reason")) or (
                isinstance(submission, dict) and submission.get("outcome") == "failed"
            )
            evidence["reason"] = (
                "retry has a recorded worker or integration failure"
                if justified
                else "retry proposal has no recorded failure"
            )
            return ("approve" if justified else "reject"), evidence
        if kind in {"task_decomposition", "dependency_change", "retry_task"}:
            evidence["reason"] = "proposal is structurally closed for journal application"
            evidence["payload"] = payload
            return "approve", evidence
        evidence["reason"] = f"unsupported proposal kind: {kind}"
        return "reject", evidence

    def _run_validation(self, envelope: dict) -> str:
        proposal_id = int(envelope["proposal_id"])
        kind = str(envelope["proposal_kind"])
        self.log(f"validating proposal {proposal_id}: {kind}")
        vote, evidence = self._validate(envelope)
        result = self.client.submit_validation(
            run_id=self.run_id,
            worker_id=self.worker_id,
            proposal_id=proposal_id,
            claim_token=str(envelope["claim_token"]),
            vote=vote,
            evidence=evidence,
        )
        self.log(
            f"validated proposal {proposal_id}: {vote} "
            f"({result.get('approvals', 0)} approve, {result.get('rejections', 0)} reject)"
        )
        return "validated"

    def run_once(self) -> str:
        envelope = self.client.claim_work(
            self.run_id,
            self.worker_id,
            self.lease_seconds,
        )
        status = str(envelope["status"])
        if status != "claimed":
            return status
        if envelope.get("work_kind") == "validation":
            return self._run_validation(envelope)
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
        self.client.submit_result(
            run_id=self.run_id,
            task_id=task_id,
            claim_token=token,
            outcome=outcome,
            summary=summary,
            commit_sha=commit,
            blockers=list(blockers),
            proposed_followups=[],
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
