import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from .evidence import (
    COMMAND_SHELL,
    EvidenceError,
    artifact_digest,
    begin_attempt,
    evidence_key,
    finish_attempt,
    load_terminal_receipt,
    observed_environment,
    validate_declared_json_receipt,
)
from .journal_backend import JournalError
from .verification.driver import engine_digest, invocation
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
        evidence_root: str | Path | None = None,
        contract_root: str | Path | None = None,
        contract_path: str = "verification/contract.toml",
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
        self.evidence_root = (
            Path(evidence_root)
            if evidence_root
            else ((self.run_config.parent / "evidence" / "checks") if self.run_config else None)
        )
        self.contract_root = (
            Path(contract_root)
            if contract_root
            else ((self.run_config.parent / "frozen-contract") if self.run_config else None)
        )
        self.contract_path = contract_path
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
        return (
            f"Perform exactly one journal-assigned role as {self.worker_id}.\nThe journal is the entire coordination interface between workers. journal_search and journal_add are the only journal operations. Use repository, Git, shell, and test tools normally for the assigned role; never inspect the harness, journal socket, or journal database by shell. Treat the target's frozen verification contract and immutable specification inputs as authority; the target contains no harness runtime implementation.\n"
            "SEARCH returns one bounded union of exact and threshold-eligible semantic records. The configured exact floor preserves exact anchors when available, but one response is not necessarily exhaustive. Results are ordered only by journal sequence; semantic score never controls position. Use match_kind and stored routing metadata to distinguish exact records, and use returned task IDs, errors, hashes, decisions, components, or concepts for iterative follow-up searches. Absence from an ordinary natural-language search is not proof that no record exists.\n"
            f"1. Search `worker:{self.worker_id}`; the host normally preclaims. If empty, search `queue:ready`, rotate the complete ready list left by {ordinal} modulo its length, and journal_add exact claims until accepted.\n2. Orient only with records bound to the assignment: REVISION searches `review:<root_task_id>:<generation>` and `verify:<root_task_id>:<head_sha>`; INTERNAL REVIEW searches the exact assigned `head_sha` and any returned acceptance-result identities; MERGE searches `review:<root_task_id>:<generation>`. Never assume one bounded response exhausts assignment history; follow useful returned identifiers with more specific searches.\n"
            "3. Keep the shared truth current with substantive work records. Immediately after orientation or a new finding, before and after a material command or diagnostic, and after each durable edit, add `work-log: <claim identity after claim: >`, `phase: <oriented|diagnosed|editing|checking|check-result|ready>`, and one concise concrete `evidence:` fact. Never add timer, heartbeat, elapsed-time, or empty progress records. Re-search the exact claim after each work-log.\n4. Follow the assigned role. A fresh session may receive any role. Acceptance-check assignments are executed directly by the qualified harness process against the immutable candidate; they never require a model session.\n"
            f"AUTHOR or REVISION\n- Inspect the repository specification directly with rg, file reads, and Git. Journal citations, discoveries, decisions, and evidence, never copied specification content.\n- Fetch origin. For a new candidate, create the assigned branch at the returned base. For a revision, check it out and merge current `origin/{self.target_branch}` before editing. There is no integration branch.\n- Diagnose, implement only the objective, and use targeted development diagnostics when useful, but do not run the returned acceptance list: publication creates shared candidate-bound check assignments. After each coherent durable edit, add `checkpoint: task:<root_task_id>` and a work-log. Commit and push the exact branch.\n- Re-search the claim, add `handoff: task:<root_task_id>` with candidate evidence, then add `open-pr: task:<root_task_id>` with exact `branch:`, `base:`, `head:`, `tree:` (from `git rev-parse HEAD^{{tree}}`), and `verified: true`. That flag attests immutable candidate publication, not acceptance-check success.\n- The deterministic judge and receipt schema are harness-owned. The target owns the frozen semantic contract, toolchain lock, formal artifacts, and coverage manifest. A candidate cannot change an immutable contract input or redefine the engine that judges it.\n- If an external capability is unavailable, journal `blocked: task:<root_task_id>` with the returned role, branch, generation, head, `verified: false`, and reason.\n"
            "INTERNAL REVIEW\n- An accepted claim is final eligibility for that generation. Worker slots are reusable and every role starts a fresh context, so current or prior authorship does not exclude a slot. Work read-only at exact `head_sha`; inspect the diff and repository specification for correctness, completeness, maintainability, tests, architectural fit, omissions, weak assertions, unsafe shortcuts, and integration hazards.\n- Apply the returned `review_role` as your primary independent question: `specification` asks whether the product model covers intended success, failure, boundary, and reachable-state behavior; `adversarial` asks whether the production implementation is actually connected to meaningful, non-vacuous proof obligations; `integration` asks whether concurrency, persistence, recovery, unsafe code, source closure, assumptions, scalability, and the trusted boundary are sound. Do not substitute one question for another.\n- Consume terminal acceptance results listed or found through exact `verify:<root_task_id>:<head_sha>:<ordinal>` searches. Review is independent of check execution: do not mechanically rerun the declared command list. Targeted diagnostics may investigate a concern; claim an unsatisfied acceptance check only as a separate fresh assignment after review. Passing review cannot replace a required check result, and passing checks cannot replace review.\n- Re-search the review identity. On pass add `approve: review:<root_task_id>:<generation>:<review_ordinal>` with exact head, `verified: true`, and criterion-level evidence for the assigned review question. On defect use `challenge:` with the same identity, exact head, `verified: true`, and reason. Do not edit, commit, or push.\n"
            f"MERGE\n- Confirm the exact head, configured approval count, and shared acceptance results. Re-search the merge identity, then add `merge: task:<root_task_id>` with exact generation and head. The launcher verifies repository and prospective-tree invariants before merging to `{self.target_branch}`.\nDo not claim a second item. Both tools take one `yaml` argument containing exactly one string field: `query` for journal_search or `text` for journal_add.\n"
        )

    def _configs(self) -> list[str]:
        # Keep the virtual-environment entry point. Resolving its symlink escapes
        # the environment and starts the MCP server without harness dependencies.
        python = str(Path(sys.executable).absolute())
        forwarded = [
            "PYTHONPATH",
            "OXIDE_JOURNAL_SOCKET",
            "OXIDE_RUN_ID",
            "OXIDE_WORKER_ID",
            "OXIDE_RUN_CONFIG",
            "OXIDE_RUN_EPOCH",
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
            'shell_environment_policy.exclude=["OXIDE_*"]',
            f"sandbox_workspace_write.writable_roots={json.dumps(writable)}",
            f"mcp_servers.journal.command={json.dumps(python)}",
            'mcp_servers.journal.args=["-m","oxide.journal_mcp"]',
            f"mcp_servers.journal.env_vars={json.dumps(forwarded)}",
            "mcp_servers.journal.required=true",
            'mcp_servers.journal.enabled_tools=["journal_add","journal_search"]',
            "mcp_servers.journal.startup_timeout_sec=5",
            "mcp_servers.journal.tool_timeout_sec=180",
        ]

    def _claim(self, ready: list[dict]) -> dict | None:
        ordinal = int(self.worker_id.rsplit("-", 1)[-1])
        if not ready:
            return None
        item = ready[ordinal % len(ready)]
        try:
            response = self.client.add(self.run_id, self.worker_id, str(item["claim"]))
        except WorkflowError:
            # Search again after losing the atomic claim.
            return None
        claimed = response.get("work")
        if not isinstance(claimed, dict):
            raise WorkflowError("accepted claim did not return the owned assignment")
        self._set_assignment(claimed)
        self.log(f"journal_add accepted: {item['claim']}")
        return claimed

    def _publish_verification_result(self, text: str) -> int:
        try:
            self.client.add(self.run_id, self.worker_id, text)
        except JournalError as error:
            self.log(f"journal_add unavailable after acceptance check: {error}")
            return 2
        return 0

    @staticmethod
    def _verification_terminal(
        item: dict,
        receipt: dict,
        receipt_digest: str,
    ) -> str:
        identity = str(item["claim"]).removeprefix("claim: ")
        result = str(receipt["result"])
        marker = {
            "passed": "verify-pass",
            "product_failure": "verify-fail",
            "infrastructure_failure": "verify-infrastructure",
        }[result]
        detail_name = "evidence" if result == "passed" else "reason"
        if result == "passed":
            detail = (
                "qualified command passed; stdout "
                f"{artifact_digest(receipt, 'stdout')}; stderr "
                f"{artifact_digest(receipt, 'stderr')}"
            )
        else:
            detail = (
                f"qualified command classified {result} with exit "
                f"{receipt.get('exit_code')}; stdout {artifact_digest(receipt, 'stdout')}; "
                f"stderr {artifact_digest(receipt, 'stderr')}"
            )
        return "\n".join(
            (
                f"{marker}: {identity}",
                f"head: {item['head_sha']}",
                f"tree: {item['tree_sha']}",
                f"base: {item['evidence_requirement']['candidate']['base']}",
                f"evidence-key: {item['evidence_key']}",
                f"claim-attempt: {item['execution_attempt']}",
                f"execution-attempt: {receipt['execution_attempt']}",
                f"receipt: {receipt_digest}",
                "verified: true",
                f"{detail_name}: {detail}",
            )
        )

    def _run_acceptance_check(self, item: dict) -> int:
        claim = str(item.get("claim", ""))
        head = str(item.get("head_sha", ""))
        tree = str(item.get("tree_sha", ""))
        task = str(item.get("root_task_id", ""))
        ordinal = item.get("verification_ordinal")
        checks = item.get("checks")
        check = item.get("check_contract")
        requirement = item.get("evidence_requirement")
        key = item.get("evidence_key")
        attempt = item.get("execution_attempt")
        expected = f"claim: verify:{task}:{head}:{ordinal}"
        if (
            item.get("role") != "verification"
            or claim != expected
            or re.fullmatch(r"[0-9a-f]{40}", head) is None
            or re.fullmatch(r"[0-9a-f]{40}", tree) is None
            or not isinstance(ordinal, int)
            or not isinstance(checks, list)
            or len(checks) != 1
            or not isinstance(checks[0], str)
            or not checks[0]
            or not isinstance(check, dict)
            or not isinstance(requirement, dict)
            or not isinstance(key, str)
            or key != evidence_key(requirement)
            or not isinstance(attempt, str)
            or re.fullmatch(r"[0-9a-f]{64}", attempt) is None
            or self.evidence_root is None
        ):
            raise WorkflowError("acceptance check assignment is malformed")
        command = checks[0]
        existing = load_terminal_receipt(
            self.evidence_root,
            requirement,
            execution_attempt=attempt,
        )
        if existing is not None:
            receipt, digest = existing
            return self._publish_verification_result(
                self._verification_terminal(item, receipt, digest)
            )

        qualification = requirement.get("qualification")
        if not isinstance(qualification, dict):
            raise WorkflowError("acceptance check qualification is malformed")
        timeout = int(qualification.get("timeout_seconds", 1800))
        maximum_artifact_bytes = int(qualification.get("max_artifact_bytes", 16777216))
        infrastructure_codes = qualification.get("infrastructure_exit_codes", [2, 124])
        expected_environment = qualification.get("environment")
        expected_engine = qualification.get("verification_engine")
        result_kind = "infrastructure_failure"
        exit_code: int | None = None
        started_at = time.time()
        begin_attempt(self.evidence_root, requirement, attempt)
        with tempfile.TemporaryDirectory(
            prefix=f"{self.worker_id}-acceptance-check-",
            dir=self.evidence_root.parent,
        ) as raw:
            temporary = Path(raw)
            repository = temporary / "repository"
            stdout = temporary / "stdout.log"
            stderr = temporary / "stderr.log"
            declared = temporary / "declared"
            declared.mkdir()
            try:
                if (
                    expected_environment is not None
                    and expected_environment != observed_environment()
                ):
                    raise EvidenceError("execution environment differs from contract qualification")
                if check.get("driver") == "verus" and expected_engine != engine_digest():
                    raise EvidenceError("verification engine changed after contract qualification")
                subprocess.run(
                    ["git", "clone", "--no-hardlinks", str(self.target_repo), str(repository)],
                    text=True,
                    capture_output=True,
                    check=True,
                )
                subprocess.run(
                    ["git", "checkout", "--detach", head],
                    cwd=repository,
                    text=True,
                    capture_output=True,
                    check=True,
                )
                actual_tree = subprocess.run(
                    ["git", "rev-parse", "HEAD^{tree}"],
                    cwd=repository,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout.strip()
                if actual_tree != tree:
                    raise EvidenceError("candidate commit does not resolve to its frozen tree")
                working_directory = (repository / str(check["working_directory"])).resolve()
                if (
                    not working_directory.is_relative_to(repository)
                    or not working_directory.is_dir()
                ):
                    raise EvidenceError("qualified check working directory is unavailable")
                environment = os.environ.copy()
                environment.update(
                    {str(k): str(v) for k, v in check.get("environment", {}).items()}
                )
                environment.update(
                    {
                        "OXIDE_FROZEN_CONTRACT_ROOT": str(self.contract_root or ""),
                        "OXIDE_CANDIDATE_COMMIT": head,
                        "OXIDE_CANDIDATE_TREE": tree,
                        "OXIDE_PROSPECTIVE_COMMIT": head,
                        "OXIDE_PROSPECTIVE_TREE": tree,
                        "OXIDE_EVIDENCE_RECEIPT": str(declared / "receipt.json"),
                        "OXIDE_EVIDENCE_ARTIFACT_DIR": str(declared / "artifacts"),
                    }
                )
                if check.get("driver") == "verus":
                    if self.contract_root is None:
                        raise EvidenceError("frozen verification contract is unavailable")
                    process_command = invocation(
                        repository,
                        self.contract_root,
                        str(check.get("operation")),
                        contract_path=self.contract_path,
                        root=check.get("root"),
                        candidate_tree=tree,
                        prospective_tree=tree,
                        receipt=declared / "receipt.json",
                        artifact_dir=declared / "artifacts",
                    )
                    process_directory = repository
                elif check.get("driver") == "command":
                    process_command = [COMMAND_SHELL, "-lc", command]
                    process_directory = working_directory
                else:
                    raise EvidenceError("acceptance check has an unsupported driver")
                self.log(f"ACCEPTANCE CHECK STARTED {claim.removeprefix('claim: ')}\n{command}")
                with stdout.open("wb") as out, stderr.open("wb") as err:
                    process = subprocess.Popen(
                        process_command,
                        cwd=process_directory,
                        env=environment,
                        stdout=out,
                        stderr=err,
                        start_new_session=True,
                    )
                    self.process = process  # lets pause/reset kill the exact command group
                    try:
                        exit_code = process.wait(timeout=timeout)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                        exit_code = 124
                    finally:
                        self.process = None
                result_kind = (
                    "passed"
                    if exit_code == 0
                    else "infrastructure_failure"
                    if exit_code in infrastructure_codes
                    else "product_failure"
                )
                if check.get("receipt_required", False):
                    validate_declared_json_receipt(
                        declared / "receipt.json",
                        maximum_bytes=maximum_artifact_bytes,
                    )
            except (OSError, subprocess.CalledProcessError, EvidenceError, ValueError) as error:
                with stderr.open("a", encoding="utf-8") as stream:
                    stream.write(f"qualified-check infrastructure failure: {error}\n")
                stdout.touch(exist_ok=True)
                result_kind = "infrastructure_failure"
                exit_code = 2
            finally:
                self.process = None
            declared_paths = [path for path in declared.rglob("*") if path.is_file()]
            for relative in check.get("artifacts", []):
                candidate = (repository / str(relative)).resolve()
                if candidate.is_relative_to(repository) and candidate.is_file():
                    declared_paths.append(candidate)
            receipt, digest = finish_attempt(
                self.evidence_root,
                requirement,
                attempt,
                result=result_kind,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                artifact_paths=declared_paths,
                maximum_artifact_bytes=maximum_artifact_bytes,
                started_at=started_at,
            )
            output = (
                stdout.read_text(encoding="utf-8", errors="replace")
                + stderr.read_text(encoding="utf-8", errors="replace")
            ).rstrip()
            if output:
                self.log(output[-12000:])
            self.log(
                f"ACCEPTANCE CHECK COMPLETED {claim.removeprefix('claim: ')} "
                f"result={receipt['result']} exit={receipt['exit_code']}"
            )
        return self._publish_verification_result(self._verification_terminal(item, receipt, digest))

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
                r"open-pr: task:|blocked: task:|(?:approve|challenge): review:|merge: task:|verify-(?:pass|fail|infrastructure): verify:",
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
            "workflow projection.\n"
        )
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHONPATH": str(Path(__file__).resolve().parents[1]),
                "OXIDE_JOURNAL_SOCKET": self.journal_socket,
                "OXIDE_RUN_ID": self.run_id,
                "OXIDE_WORKER_ID": self.worker_id,
                "OXIDE_RUN_CONFIG": str(self.run_config) if self.run_config else "",
                "OXIDE_RUN_EPOCH": str(self.epoch),
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
        item: dict
        if not active:
            claimed = self._claim(ready)
            if claimed is None:
                # The next cycle reconstructs readiness after contention.
                return "contended"
            item = claimed
        else:
            item = active[0]
            self._set_assignment(item)
        code = (
            self._run_acceptance_check(item)
            if item.get("role") == "verification"
            else self._codex()
        )
        if code:
            self.log(f"worker action exited {code}; the slot will reconstruct from the journal")
            return "retry"
        return "worked"

    def run(self, idle_seconds: float = 1.0) -> str:
        while True:
            state = self.run_once()
            if state in {"publishing", "complete", "paused", "stopped", "failed"}:
                return state
            if state != "worked":
                time.sleep(idle_seconds)
