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


def worktree_diff(repository: str | Path, paths: Sequence[object]) -> tuple[list[str], str]:
    """Return a display-safe Git patch for the changed paths in a worker worktree."""
    root = Path(repository).resolve()
    relative: list[Path] = []
    for raw_path in paths:
        candidate = Path(str(raw_path))
        if not candidate.is_absolute():
            candidate = root / candidate
        try:
            path = candidate.resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if path == Path(".") or path in relative:
            continue
        relative.append(path)
    if not relative or not (root / ".git").exists():
        return [str(path) for path in relative], ""

    names = [str(path) for path in relative]
    try:
        tracked = subprocess.run(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-color",
                "--unified=3",
                "HEAD",
                "--",
                *names,
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        patches = [tracked.stdout.rstrip()] if tracked.stdout.strip() else []
        for path, name in zip(relative, names, strict=True):
            known = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", name],
                cwd=root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
            if known.returncode == 0 or not (root / path).is_file():
                continue
            added = subprocess.run(
                [
                    "git",
                    "diff",
                    "--no-index",
                    "--no-ext-diff",
                    "--no-color",
                    "--unified=3",
                    "--",
                    "/dev/null",
                    name,
                ],
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
            )
            if added.stdout.strip():
                patches.append(added.stdout.rstrip())
    except OSError:
        return names, ""
    return names, "\n".join(patches)


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
        journal_socket: str | Path,
        codex_argv: Sequence[str] = ("codex", "exec"),
        model: str | None = None,
        assignment_path: str | Path | None = None,
        run_config: str | Path | None = None,
        epoch: int = 0,
        log: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.run_id = run_id
        self.worker_id = worker_id
        self.repository = Path(repository)
        self.target_branch = target_branch
        self.target_repo = Path(target_repo)
        self.journal_socket = str(journal_socket)
        self.codex_argv = list(codex_argv)
        self.model = model
        self.assignment_path = Path(assignment_path) if assignment_path else None
        self.run_config = Path(run_config) if run_config else None
        self.epoch = epoch
        self.process: subprocess.Popen[str] | None = None
        self.log = log

    def stop(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def _set_assignment(self, item: dict) -> None:
        if self.assignment_path is None:
            return
        self.assignment_path.parent.mkdir(parents=True, exist_ok=True)
        self.assignment_path.write_text(
            ("implementation" if item["role"] in {"author", "revision"} else str(item["role"]))
            + f"\nepoch:{self.epoch}\n",
            encoding="utf-8",
        )

    def _prompt(self) -> str:
        ordinal = int(self.worker_id.rsplit("-", 1)[-1])
        return f"""Perform exactly one journal-assigned role as {self.worker_id}.\nThe journal is the entire coordination interface between workers. journal_search and journal_add are the only journal operations. Use repository, Git, shell, and test tools normally for the assigned role; never inspect the harness, journal socket, or journal database by shell. Treat the repository's swarm-harness directory as immutable workload input, never as product implementation.\nSEARCH returns one bounded union of exact and threshold-eligible semantic records. The configured exact floor preserves exact anchors when available, but one response is not necessarily exhaustive. Results are ordered only by journal sequence; semantic score never controls position. Use match_kind and stored routing metadata to distinguish exact records, and use returned task IDs, errors, hashes, decisions, components, or concepts for iterative follow-up searches. Absence from an ordinary natural-language search is not proof that no record exists.\n1. Search `worker:{self.worker_id}`; the host normally preclaims. If empty, search `queue:ready`, prefer implementation work, rotate that group left by {ordinal} modulo its length, and journal_add exact claims until accepted.\n2. Orient only with records bound to the assignment: REVISION searches `review:<root_task_id>:<generation>` and `verify:<root_task_id>:<head_sha>`; INTERNAL REVIEW searches the exact assigned `head_sha`; VERIFICATION searches its exact claim identity; MERGE searches `review:<root_task_id>:<generation>`. Never assume one bounded response exhausts assignment history; follow useful returned identifiers with more specific searches.\n3. Keep the shared truth current with substantive work records. Immediately after orientation or a new finding, before and after every command or check batch that can materially affect the outcome, and after each durable edit, add:\n  work-log: <exact claim identity after `claim: `>\n  phase: <oriented|diagnosed|editing|checking|check-result|ready>\n  evidence: <one concise concrete fact, file/change, command, or result>\nNever add timer, heartbeat, elapsed-time, or empty progress records. Each work-log must communicate new reusable information. After every work-log, search the exact claim identity again before beginning the next phase so concurrent facts and invalidation are observed.\n4. Follow the assigned role. A fresh session may receive any role.\nAUTHOR or REVISION\n- Inspect the checked-out repository specification directly with rg, file reads, and Git. Journal citations, discoveries, decisions, and evidence, never copied roadmap or specification content.\n- Fetch origin. For a new PR, create the assigned branch at the returned base. For a revision, check it out and merge current `origin/{self.target_branch}` before editing. There is no integration branch.\n- Journal the concrete diagnosis before editing. Implement only the objective. After each coherent durable edit, add both `checkpoint: task:<root_task_id>` with concise files/status evidence and a `work-log` editing record. Before each returned check, journal the exact command as checking; immediately journal its pass/fail result before continuing. Fix failures, commit, and push HEAD to the exact branch.\n- Search the exact claim once more, then add `handoff: task:<root_task_id>` with files and check evidence, followed by:\n  open-pr: task:<root_task_id>\n  branch: <exact assigned branch>\n  base: <exact commit the candidate is based on>\n  head: <exact pushed HEAD>\n  verified: true\n- If a required external capability is unavailable, journal this exact terminal form:\n  blocked: task:<root_task_id>\n  role: <returned role>\n  branch: <returned branch>\n  generation: <returned generation>\n  head: <returned head>\n  verified: false\n  reason: <concise reason>\nINTERNAL REVIEW\n- An accepted claim is final eligibility for that generation. Worker slots are reusable and every role starts a fresh context, so current or prior authorship does not exclude a slot. Review the exact assigned head.\n- Work read-only. Fetch the branch and detach at exact `head_sha`; journal the inspected base-to-head diff finding. Before each returned check, journal the exact command as checking and immediately journal its result. Re-search the exact review identity before deciding. Do not edit, commit, or push.\n- On pass, add the exact review identity from the work item:\n  approve: review:<root_task_id>:<generation>:<review_ordinal>\n  head: <exact reviewed head>\n  verified: true\n  evidence: <criterion-level evidence>\n- On any defect use `challenge:` with the same identity, exact head, `verified: true`, and `reason:`. Finish your decision even if a sibling challenged; a changed candidate requires all reviews again.\nVERIFICATION\n- Work read-only. Fetch origin and detach at the exact assigned `head_sha`. Journal the exact acceptance command as checking, run it, immediately journal the concrete result, then re-search the exact verification identity before the terminal decision. Do not edit, commit, or push.\n- If `last_error` is present, this is revision support: inspect that defect alongside the check and include concrete repair guidance in the result work-log and terminal evidence.\n- On pass add:\n  verify-pass: <exact claim identity after `claim: `>\n  head: <exact assigned head>\n  verified: true\n  evidence: <command result and, when applicable, targeted repair guidance>\n- On failure use `verify-fail:` with the same identity, exact head, `verified: true`, and `reason:`.\nMERGE\n- Confirm the exact head and configured approval count from the bounded orientation search, journal that concrete authorization evidence, and re-search the exact merge identity. Add `merge: task:<root_task_id>` with exact `generation:` and `head:`. The launcher verifies the prospective tree before merging to `{self.target_branch}`.\nDo not claim a second item. Finish after the terminal record is accepted.\nBoth tools take one `yaml` argument containing exactly one string field: `query` for journal_search or `text` for journal_add.\n"""

    def _configs(self) -> list[str]:
        # Keep the virtual-environment entry point. Resolving its symlink escapes
        # the environment and starts the MCP server without harness dependencies.
        python = str(Path(sys.executable).absolute())
        forwarded = [
            "PYTHONPATH",
            "SWARM_JOURNAL_SOCKET",
            "SWARM_RUN_ID",
            "SWARM_WORKER_ID",
            "SWARM_RUN_CONFIG",
            "SWARM_RUN_EPOCH",
        ]
        writable = [
            str((self.repository / ".git").resolve()),
            str((self.target_repo / ".git").resolve()),
            str((Path.home() / ".docker" / "run").resolve()),
        ]
        return [
            'approval_policy="never"',
            'web_search="disabled"',
            "sandbox_workspace_write.network_access=true",
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
        groups = (
            [item for item in ready if item.get("role") != "verification"],
            [item for item in ready if item.get("role") == "verification"],
        )
        ordinal = int(self.worker_id.rsplit("-", 1)[-1])
        for group in groups:
            eligible = group
            if not eligible:
                continue
            item = eligible[ordinal % len(eligible)]
            try:
                self.client.add(self.run_id, self.worker_id, str(item["claim"]))
            except WorkflowError:
                # The snapshot is stale after a losing atomic claim. Search again
                # before proposing any other ownership record.
                return False
            self._set_assignment(item)
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
            and re.search(
                r"open-pr: task:|blocked: task:|(?:approve|challenge): review:|merge: task:|verify-(?:pass|fail): verify:",
                text,
            )
            is not None
        )

    def _enrich_log_line(self, line: str) -> str:
        """Persist the patch alongside a completed file-change event."""
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return line
        if not isinstance(event, dict) or event.get("type") != "item.completed":
            return line
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "file_change":
            return line
        changes = item.get("changes")
        if not isinstance(changes, list):
            return line
        paths = [
            change["path"] for change in changes if isinstance(change, dict) and change.get("path")
        ]
        relative_paths, patch = worktree_diff(self.repository, paths)
        if relative_paths:
            item["relative_paths"] = relative_paths
        if patch:
            item["patch"] = patch
        return json.dumps(event, ensure_ascii=False, separators=(",", ":"))

    def _codex(self) -> int:
        argv = [
            *self.codex_argv,
            "--ignore-user-config",
            "--strict-config",
            "--ignore-rules",
            "-C",
            str(self.repository),
            "--sandbox",
            "danger-full-access",
            "--ephemeral",
            "--json",
        ]
        for config in self._configs():
            argv.extend(("-c", config))
        if self.model:
            argv.extend(("--model", self.model))
        argv.append(
            self._prompt() + "\nWhen re-searching after a work-log, query its complete first line "
            "(`work-log: <claim identity>`), not the bare `task:` identity. This "
            "retrieves only assignment-bound communication without replaying the "
            "workflow projection. If the returned claim is `claim: verify:X`, the "
            "first line is exactly `work-log: verify:X`; never write "
            "`work-log: claim: verify:X`.\n"
            + "\nA failing acceptance command is implementation work, not an unavailable "
            "external capability. Diagnose and repair the first concrete failure rather "
            "than recording an environmental blocker unless the required capability is "
            "actually absent.\n"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "SWARM_JOURNAL_SOCKET": self.journal_socket,
                "SWARM_RUN_ID": self.run_id,
                "SWARM_WORKER_ID": self.worker_id,
                "SWARM_RUN_CONFIG": str(self.run_config) if self.run_config else "",
                "SWARM_RUN_EPOCH": str(self.epoch),
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
        self.process = process
        assert process.stdout is not None
        try:
            for line in process.stdout:
                raw_line = line.rstrip("\n")
                self.log(self._enrich_log_line(raw_line))
                if self._terminal_record(line):
                    os.killpg(process.pid, signal.SIGKILL)
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
        finally:
            self.process = None

    def run_once(self) -> str:
        state, active, ready = self.client.worker_snapshot(self.run_id, self.worker_id)
        if state != "running":
            return str(state)
        if not active and not ready:
            return "idle"
        if not active and not self._claim(ready):
            # The losing claim was itself the atomic concurrency result. Do not
            # reuse or immediately replace that snapshot; the next host cycle
            # reconstructs current readiness before proposing another claim.
            return "contended"
        elif active:
            self._set_assignment(active[0])
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
