"""Replaceable Codex slot that coordinates exclusively through two journal tools."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .journal import JournalClient


class WorkerError(RuntimeError):
    pass


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise WorkerError(result.stderr.strip() or "Git command failed")
    return result.stdout.strip()


class Worker:
    def __init__(
        self,
        client: JournalClient,
        run_id: str,
        worker_id: str,
        repository: str | Path,
        integration_branch: str,
        target_repo: str | Path,
        *,
        codex_argv: Sequence[str] = ("codex", "exec"),
        model: str | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.worker_id = worker_id
        self.repository = Path(repository)
        self.integration_branch = integration_branch
        self.target_repo = Path(target_repo)
        self.codex_argv = list(codex_argv)
        self.model = model
        self.log = log

    def _sync(self) -> None:
        if _git(self.repository, "status", "--porcelain=v1", "--untracked-files=all"):
            return
        remote = "refs/remotes/origin/swarm-integration"
        _git(
            self.repository,
            "fetch",
            "origin",
            f"refs/heads/{self.integration_branch}:{remote}",
        )
        _git(self.repository, "checkout", "-B", "swarm-worker", remote)

    def _prompt(self) -> str:
        return f"""Implement one Stage task as {self.worker_id}.

The journal is the entire coordination interface. You have exactly two journal
tools: journal_search and journal_add. Never open or inspect the harness,
journal socket, or journal database by shell command.

1. Call journal_search with `query: worker:{self.worker_id}`. If it returns a
   working task, resume it and search `task:<id>` for its checkpoints.
2. Otherwise call journal_search with `query: queue:ready`, choose one task, and
   atomically claim it with journal_add text whose first line is exactly
   `claim: task:<id>`. If the claim conflicts, search again and choose another.
3. Implement only that task in this clone. After the first durable edit, call
   journal_add with first line `checkpoint: task:<id>` and summarize the state.
4. Run every check returned in the task record. Fix failures. Commit all task
   files to Git.
5. Integrate without a coordinator: fetch
   `refs/heads/{self.integration_branch}` from origin, rebase your commit onto
   it, rerun the task checks, and push HEAD to that same branch. A rejected push
   means another worker won the race; fetch, rebase, recheck, and retry.
6. Call journal_add with first line `handoff: task:<id>` and record files,
   checks, risks, and the pushed commit. Then call journal_add once more with:

   complete: task:<id>
   commit: <the exact 40-character pushed HEAD>
   verified: true

Do not claim a second task in this session. Do not edit roadmap checkboxes
unless the claimed task explicitly requires them. Finish after the completion
record is accepted.

Both tools take one `yaml` argument. Its value is YAML containing exactly one
string field: `query` for journal_search or `text` for journal_add.
"""

    def _configs(self) -> list[str]:
        python = str(Path(sys.executable).resolve())
        forwarded = [
            "PYTHONPATH",
            "SWARM_JOURNAL_SOCKET",
            "SWARM_RUN_ID",
            "SWARM_WORKER_ID",
        ]
        writable = [
            str((self.repository / ".git").resolve()),
            str((self.target_repo / ".git").resolve()),
        ]
        return [
            'approval_policy="never"',
            'web_search="disabled"',
            'shell_environment_policy.exclude=["SWARM_*"]',
            f"sandbox_workspace_write.writable_roots={json.dumps(writable)}",
            f"mcp_servers.journal.command={json.dumps(python)}",
            'mcp_servers.journal.args=["-m","swarm_harness.journal_mcp"]',
            f"mcp_servers.journal.env_vars={json.dumps(forwarded)}",
            "mcp_servers.journal.required=true",
            'mcp_servers.journal.enabled_tools=["journal_add","journal_search"]',
            "mcp_servers.journal.startup_timeout_sec=5",
            "mcp_servers.journal.tool_timeout_sec=10",
        ]

    def _codex(self) -> int:
        argv = [
            *self.codex_argv,
            "--ignore-user-config",
            "--strict-config",
            "--ignore-rules",
            "-C",
            str(self.repository),
            "--sandbox",
            "workspace-write",
            "--ephemeral",
            "--json",
        ]
        for config in self._configs():
            argv.extend(("-c", config))
        if self.model:
            argv.extend(("--model", self.model))
        argv.append(self._prompt())
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "SWARM_JOURNAL_SOCKET": self.client.socket_path,
                "SWARM_RUN_ID": self.run_id,
                "SWARM_WORKER_ID": self.worker_id,
            }
        )
        process = subprocess.Popen(
            argv,
            cwd=self.repository,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        assert process.stdout is not None
        try:
            for line in process.stdout:
                self.log(line.rstrip("\n"))
            return process.wait()
        except BaseException:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            raise

    def run_once(self) -> str:
        state = self.client.search(self.run_id, "run:state")[0]["state"]
        if state != "running":
            return str(state)
        active = self.client.search(self.run_id, f"worker:{self.worker_id}")
        ready = self.client.search(self.run_id, "queue:ready")
        if not active and not ready:
            return "idle"
        if not active:
            self._sync()
        code = self._codex()
        if code:
            self.log(f"Codex exited {code}; the same slot will reconstruct from the journal")
        return "worked"

    def run(self, idle_seconds: float = 1.0) -> str:
        while True:
            state = self.run_once()
            if state in {"publishing", "complete", "paused", "stopped", "failed"}:
                return state
            time.sleep(idle_seconds)
