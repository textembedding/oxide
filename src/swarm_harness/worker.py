import json
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .workflow import WorkflowClient, WorkflowError


class Worker:
    def __init__(
        self,
        client: WorkflowClient,
        run_id: str,
        worker_id: str,
        repository: str | Path,
        target_branch: str,
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
        self.target_branch = target_branch
        self.target_repo = Path(target_repo)
        self.codex_argv = list(codex_argv)
        self.model = model
        self.log = log

    def _prompt(self) -> str:
        ordinal = int(self.worker_id.rsplit("-", 1)[-1])
        return f"""Perform exactly one journal-assigned role as {self.worker_id}.\nThe journal is the entire coordination interface. You have exactly journal_search and journal_add. Never inspect the harness, journal socket, or journal database by shell.\n1. Search `worker:{self.worker_id}`; the host normally preclaims. If empty, search `queue:ready`, rotate that list left by {ordinal} modulo its length, and journal_add exact claims until accepted. Then search `task:<root_task_id>`.\n2. Follow the assigned role. A fresh session may receive any role.\nAUTHOR or REVISION\n- Fetch origin. For a new PR, create the assigned branch at the returned base. For a revision, check it out and merge current `origin/{self.target_branch}` before editing. There is no integration branch.\n- Implement only the objective. Add `checkpoint: task:<root_task_id>` after the first durable edit. Run every returned check, fix failures, commit, and push HEAD to the exact branch.\n- Add `handoff: task:<root_task_id>` with files and check evidence, then add:\n  open-pr: task:<root_task_id>\n  branch: <exact assigned branch>\n  base: <exact commit the candidate is based on>\n  head: <exact pushed HEAD>\n  verified: true\nINTERNAL REVIEW\n- An accepted claim is final eligibility for that generation. Only its current author is excluded; prior-generation authorship is allowed. Review the exact assigned head.\n- Work read-only. Fetch the branch, detach at exact `head_sha`, inspect the complete base-to-head diff, and run every returned check. Do not edit, commit, or push.\n- On pass, add the exact review identity from the work item:\n  approve: review:<root_task_id>:<generation>:<review_ordinal>\n  head: <exact reviewed head>\n  verified: true\n  evidence: <criterion-level evidence>\n- On any defect use `challenge:` with the same identity, exact head, `verified: true`, and `reason:`. Finish your decision even if a sibling challenged; a changed candidate requires all reviews again.\nMERGE\n- Re-search and confirm the exact head and configured approval count. Add `merge: task:<root_task_id>` with exact `generation:` and `head:`. The launcher verifies the prospective tree before merging to `{self.target_branch}`.\nDo not claim a second item. Finish after the terminal record is accepted.\nBoth tools take one `yaml` argument containing exactly one string field: `query` for journal_search or `text` for journal_add.\n"""

    def _configs(self) -> list[str]:
        python = str(Path(sys.executable).resolve())
        forwarded = ["PYTHONPATH", "SWARM_JOURNAL_SOCKET", "SWARM_RUN_ID", "SWARM_WORKER_ID"]
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

    def _claim(self, ready: list[dict]) -> bool:
        offset = int(self.worker_id.rsplit("-", 1)[-1]) % len(ready)
        for item in ready[offset:] + ready[:offset]:
            try:
                self.client.add(self.run_id, self.worker_id, str(item["claim"]))
            except WorkflowError:
                continue
            self.log(f"journal_add accepted: {item['claim']}")
            return True
        return False

    @staticmethod
    def _terminal_record(line: str) -> bool:
        try:
            item = json.loads(line).get("item", {})
        except (json.JSONDecodeError, AttributeError):
            return False
        if not isinstance(item, dict):
            return False
        arguments = item.get("arguments", {})
        text = str(arguments.get("yaml", "")) if isinstance(arguments, dict) else ""
        return (
            (item.get("server"), item.get("tool"), item.get("status"))
            == ("journal", "journal_add", "completed")
            and "saved: true" in json.dumps(item.get("result"))
            and re.search(r"open-pr: task:|(?:approve|challenge): review:|merge: task:", text)
            is not None
        )

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
                if self._terminal_record(line):
                    os.killpg(process.pid, signal.SIGTERM)
                    process.wait()
                    return 0
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
        if not active and not self._claim(ready):
            ready = self.client.search(self.run_id, "queue:ready")
            if not ready or not self._claim(ready):
                return "contended"
        code = self._codex()
        if code:
            self.log(f"Codex exited {code}; the same slot will reconstruct from the journal")
            return "retry"
        return "worked"

    def run(self, idle_seconds: float = 1.0) -> str:
        while True:
            state = self.run_once()
            if state in {"publishing", "complete", "paused", "stopped", "failed"}:
                return state
            if state != "worked":
                time.sleep(idle_seconds)
