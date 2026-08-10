import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pygments import highlight as pygments_highlight
from pygments.formatters import TerminalFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from .concurrency import ConcurrencyError, implementation_digest, run_campaign, validate_receipt
from .contract import ContractError, load_contract
from .evidence import (
    COMMAND_SHELL,
    EvidenceError,
    begin_attempt,
    canonical_bytes,
    evidence_key,
    finish_attempt,
    load_terminal_receipt,
    observed_environment,
    sha256_bytes,
    validate_declared_json_receipt,
)
from .journal_backend import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_EXACT,
    JournalError,
    JournalPort,
    JournalTimeoutError,
    connect_journal,
    start_journal,
    validate_search_capacity,
)
from .verification.driver import engine_digest, invocation
from .worker import Worker, worktree_diff
from .workflow import WorkflowClient, WorkflowError

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / ".oxide" / "runs"
CHECKPOINTS = ROOT / ".oxide" / "checkpoints"
TARGET_VERIFICATION_DIRECTORY = "verification"
_WORKLOAD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")


class HarnessError(RuntimeError):
    pass


def _run_dir(workload: str) -> Path:
    if _WORKLOAD_NAME.fullmatch(workload) is None:
        raise HarnessError("workload must be a safe name, not a path")
    return RUNS / workload


def _config_path(workload: str) -> Path:
    return _run_dir(workload) / "run.json"


def _contract_path(target: str | Path, contract: str) -> Path:
    target_root = Path(target).resolve()
    root = (target_root / TARGET_VERIFICATION_DIRECTORY).resolve()
    if not root.is_relative_to(target_root):
        raise HarnessError("target verification directory must not escape through a symlink")
    relative = Path(contract)
    if relative.is_absolute() or ".." in relative.parts:
        raise HarnessError("verification contract must be a target-relative path")
    path = (target_root / relative).resolve()
    if not path.is_relative_to(root):
        raise HarnessError("verification contract must live under verification/")
    return path


def _repository_identity(target: Path) -> str:
    remote = _git(target, "remote", "get-url", "origin", check=False)
    return remote or str(target.resolve())


def _immutable_entries(target: Path, base_commit: str, paths: list[str]) -> list[dict[str, str]]:
    raw = subprocess.run(
        ["git", "ls-tree", "-r", "-z", "--full-tree", base_commit, "--", *paths],
        cwd=target,
        capture_output=True,
        check=False,
    )
    if raw.returncode:
        raise HarnessError(raw.stderr.decode(errors="replace").strip() or "cannot freeze contract")
    entries: list[dict[str, str]] = []
    for item in raw.stdout.split(b"\0"):
        if not item:
            continue
        try:
            header, encoded_path = item.split(b"\t", 1)
            mode, kind, object_id = header.decode("ascii").split()
            path = encoded_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as error:
            raise HarnessError("contract closure contains an unsupported Git entry") from error
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise HarnessError(f"immutable contract path is not a regular tracked file: {path}")
        entries.append({"path": path, "mode": mode, "blob": object_id})
    for configured in paths:
        prefix = configured.rstrip("/")
        if not any(
            item["path"] == prefix or item["path"].startswith(prefix + "/") for item in entries
        ):
            raise HarnessError(
                f"immutable contract path is absent from the staged base: {configured}"
            )
    if not entries:
        raise HarnessError("immutable contract closure is empty")
    return sorted(entries, key=lambda item: item["path"])


def _frozen_workload_ref(
    target: Path,
    base_commit: str,
    contract_path: Path,
    stage: dict[str, Any],
) -> dict[str, Any]:
    relative = contract_path.relative_to(target).as_posix()
    blob = _git(target, "rev-parse", f"{base_commit}:{relative}")
    verification = stage["verification"]
    immutable_paths = list(verification["immutable_paths"])
    if relative not in immutable_paths:
        raise HarnessError("contract.immutable_paths must include the contract itself")
    entries = _immutable_entries(target, base_commit, immutable_paths)
    result: dict[str, Any] = {
        "schema": "OxideVerificationContractRefV1",
        "target_repository": _repository_identity(target),
        "base_commit": base_commit,
        "contract_path": relative,
        "contract_blob": blob,
        "immutable_paths": immutable_paths,
        "immutable_entries": entries,
        "contract_closure_sha256": sha256_bytes(canonical_bytes(entries)),
        "verification_engine_sha256": engine_digest(),
        "harness_version": implementation_digest(ROOT),
        "verification": {
            "schema": "OxideFrozenVerificationPolicyV1",
            "evidence_policy": str(verification["evidence_policy"]),
            "timeout_seconds": int(verification["timeout_seconds"]),
            "infrastructure_exit_codes": list(verification["infrastructure_exit_codes"]),
            "max_artifact_bytes": int(verification["max_artifact_bytes"]),
            "prospective_receipt_required": bool(verification["prospective_receipt_required"]),
        },
    }
    return result


def _materialize_contract(config: dict[str, Any]) -> Path:
    reference = config["workload_ref"]
    target = Path(config["target_repo"])
    root = Path(config["contract_root"]).resolve()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    base = str(config["base_commit"])
    for entry in reference["immutable_entries"]:
        destination = (root / str(entry["path"])).resolve()
        if not destination.is_relative_to(root):
            raise HarnessError("contract entry escaped its frozen root")
        blob = subprocess.run(
            ["git", "show", f"{base}:{entry['path']}"],
            cwd=target,
            capture_output=True,
            check=False,
        )
        if blob.returncode:
            raise HarnessError(
                blob.stderr.decode(errors="replace").strip()
                or f"cannot materialize contract entry {entry['path']}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(blob.stdout)
        destination.chmod(0o755 if entry["mode"] == "100755" else 0o644)
    actual = [
        {
            "path": entry["path"],
            "mode": entry["mode"],
            "blob": _git(target, "rev-parse", f"{base}:{entry['path']}"),
        }
        for entry in reference["immutable_entries"]
    ]
    if sha256_bytes(canonical_bytes(actual)) != reference["contract_closure_sha256"]:
        raise HarnessError("materialized contract closure identity changed")
    return root


def _load_frozen_stage(config: dict[str, Any]) -> dict[str, Any]:
    target = Path(str(config["target_repo"])).resolve()
    reference = config.get("workload_ref")
    if not isinstance(reference, dict):
        raise HarnessError("run has no frozen workload reference")
    base = str(reference.get("base_commit", ""))
    path = str(reference.get("contract_path", ""))
    if reference.get("target_repository") != _repository_identity(target):
        raise HarnessError("target repository identity changed since run creation")
    if _git(target, "rev-parse", f"{base}:{path}") != reference.get("contract_blob"):
        raise HarnessError("frozen contract blob no longer matches the staged base")
    entries = reference.get("immutable_entries")
    if not isinstance(entries, list):
        raise HarnessError("run has no immutable contract closure")
    for entry in entries:
        if _git(target, "rev-parse", f"HEAD:{entry['path']}", check=False) != entry["blob"]:
            raise HarnessError("immutable verification contract changed; start a new run")
    if _git(
        target,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        *reference["immutable_paths"],
    ):
        raise HarnessError("immutable verification contract changed; start a new run")
    frozen_path = Path(config["contract_root"]) / path
    try:
        return load_contract(frozen_path)
    except ContractError as error:
        raise HarnessError(str(error)) from error


def _workflow_client(config: dict[str, Any], journal: JournalPort) -> WorkflowClient:
    stage = _load_frozen_stage(config)
    stage["required_reviews"] = int(config["required_reviews"])
    for task in stage["tasks"]:
        task["branch"] = f"{config['branch_prefix']}/{_slug(str(task['id']))}"
    return WorkflowClient(
        journal,
        stage,
        dict(config["workload_ref"]),
        replay_root=str(config["replay_root"]),
        epoch=int(config["epoch"]),
        history_sequence=int(config["history_sequence"]),
        epoch_frontiers=[dict(item) for item in config["epoch_frontiers"]],
        serialization_path=Path(config["run_dir"]) / "workflow.lock",
    )


def _valid_epoch_frontiers(value: object, epoch: object, history_sequence: object) -> bool:
    if (
        isinstance(epoch, bool)
        or not isinstance(epoch, int)
        or epoch < 0
        or isinstance(history_sequence, bool)
        or not isinstance(history_sequence, int)
        or history_sequence < 0
        or not isinstance(value, list)
    ):
        return False
    prior_epoch = -1
    prior_sequence = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"epoch", "through"}:
            return False
        item_epoch, through = item["epoch"], item["through"]
        if (
            isinstance(item_epoch, bool)
            or not isinstance(item_epoch, int)
            or not prior_epoch < item_epoch < epoch
            or isinstance(through, bool)
            or not isinstance(through, int)
            or through <= prior_sequence
        ):
            return False
        prior_epoch, prior_sequence = item_epoch, through
    return prior_sequence == history_sequence


def _load_config(workload: str) -> dict[str, Any]:
    path = _config_path(workload)
    if not path.is_file():
        raise HarnessError(f"no {workload!r} run exists; start it with harness run")
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise HarnessError(f"run configuration is unreadable: {path}") from error
    if not isinstance(config, dict):
        raise HarnessError("run configuration must be an object")
    run_id = config.get("run_id")
    expected_prefix = f"codex/oxide-{_slug(str(run_id))}"
    if (
        config.get("schema_version") != 9
        or config.get("workload") != workload
        or not isinstance(run_id, str)
        or re.fullmatch(re.escape(workload) + r"-\d{8}-\d{6}", run_id) is None
        or config.get("branch_prefix") != expected_prefix
        or not isinstance(config.get("target_repo"), str)
        or not isinstance(config.get("target_branch"), str)
        or re.fullmatch(r"[0-9a-f]{40}", str(config.get("base_commit", ""))) is None
        or not _valid_git_identity(config.get("git_identity"))
        or not isinstance(config.get("workers"), int)
        or not 1 <= int(config["workers"]) <= 64
        or not isinstance(config.get("required_reviews"), int)
        or not 1 <= int(config["required_reviews"]) <= 16
        or not isinstance(config.get("journal_command"), list)
        or not all(isinstance(value, str) for value in config["journal_command"])
        or not isinstance(config.get("concurrency_validation"), dict)
        or not isinstance(config.get("workload_ref"), dict)
        or not isinstance(config.get("evidence_root"), str)
        or not isinstance(config.get("contract_root"), str)
        or re.fullmatch(r"[0-9a-f]{32}", str(config.get("replay_root", ""))) is None
        or not _valid_epoch_frontiers(
            config.get("epoch_frontiers"),
            config.get("epoch"),
            config.get("history_sequence"),
        )
        or not isinstance(config.get("min_exact"), int)
        or not isinstance(config.get("max_results"), int)
    ):
        raise HarnessError("run configuration failed integrity validation")
    try:
        validate_search_capacity(int(config["min_exact"]), int(config["max_results"]))
    except JournalError as error:
        raise HarnessError(str(error)) from error
    run_dir = _run_dir(workload).resolve()
    if Path(str(config.get("run_dir", ""))).resolve() != run_dir:
        config = dict(config)
        config["run_dir"] = str(run_dir)
        config["database"] = str(run_dir / "journal.sqlite3")
        config["socket"] = str(run_dir / "journal.sock")
        config["evidence_root"] = str(run_dir / "evidence" / "checks")
        config["contract_root"] = str(run_dir / "frozen-contract")
    elif (
        Path(str(config["evidence_root"])).resolve() != run_dir / "evidence" / "checks"
        or Path(str(config["contract_root"])).resolve() != run_dir / "frozen-contract"
    ):
        raise HarnessError("run evidence or contract root failed integrity validation")
    target = Path(str(config.get("target_repo", ""))).resolve()
    config["contract_path"] = str(
        _contract_path(target, str(config["workload_ref"]["contract_path"]))
    )
    return config


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


@contextmanager
def _exclusive_run(workload: str):
    path = ROOT / ".oxide" / "locks" / f"{workload}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class _Log:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("a", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()

    def __call__(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        with self.lock:
            self.stream.write(line + "\n")
            try:
                print(line, flush=True)
            except BrokenPipeError:
                pass


def _git(repository: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and result.returncode:
        raise HarnessError(result.stderr.strip() or "Git command failed")
    return result.stdout.strip()


def _git_succeeds(repository: Path, *arguments: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _valid_git_identity(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"name", "email"}:
        return False
    name, email = value["name"], value["email"]
    return (
        isinstance(name, str)
        and isinstance(email, str)
        and bool(name.strip())
        and bool(email.strip())
        and "@" in email
        and not any(character in name + email for character in "\x00\r\n")
    )


def _target_git_identity(target: Path) -> dict[str, str]:
    identity = {
        "name": _git(target, "config", "--get", "user.name", check=False),
        "email": _git(target, "config", "--get", "user.email", check=False),
    }
    if not _valid_git_identity(identity):
        raise HarnessError(
            "target repository needs a valid Git identity; configure user.name and user.email"
        )
    return identity


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


def _prepare_repositories(config: dict[str, Any]) -> None:
    target = Path(config["target_repo"])
    if _git(target, "rev-parse", "--is-inside-work-tree") != "true":
        raise HarnessError("target must be a Git worktree")
    if Path(_git(target, "rev-parse", "--show-toplevel")).resolve() != target.resolve():
        raise HarnessError("target must be the Git worktree root")
    if (
        _git(target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        != config["target_branch"]
    ):
        raise HarnessError("target must remain on its staged branch")
    if not _git_succeeds(target, "merge-base", "--is-ancestor", config["base_commit"], "HEAD"):
        raise HarnessError("target branch no longer descends from the staged base")
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise HarnessError("target worktree is not clean; commit, stash, or remove changes first")
    worker_root = Path(config["run_dir"]) / "workers"
    worker_root.mkdir(parents=True, exist_ok=True)
    identity = dict(config["git_identity"])
    for index in range(int(config["workers"])):
        clone = worker_root / f"worker-{index}"
        if not clone.exists():
            result = subprocess.run(
                ["git", "clone", "--no-hardlinks", str(target), str(clone)],
                text=True,
                capture_output=True,
                check=False,
            )
            if result.returncode:
                raise HarnessError(result.stderr.strip() or "could not clone worker repository")
        _git(clone, "config", "user.name", identity["name"])
        _git(clone, "config", "user.email", identity["email"])
        remote = "refs/remotes/origin/oxide-base"
        _git(
            clone,
            "fetch",
            "origin",
            f"refs/heads/{config['target_branch']}:{remote}",
        )
        if not _git(clone, "status", "--porcelain=v1", "--untracked-files=all"):
            _git(clone, "checkout", "-B", "oxide-worker", remote)


def _run_qualified_check(
    config: dict[str, Any],
    repository: Path,
    check: dict[str, Any],
    requirement: dict[str, Any],
    *,
    candidate_commit: str,
    candidate_tree: str,
    prospective_commit: str,
    prospective_tree: str,
    receipt_required: bool = False,
) -> tuple[dict[str, Any], str]:
    root = Path(config["evidence_root"])
    existing = load_terminal_receipt(root, requirement)
    if existing is not None:
        return existing
    policy = config["workload_ref"].get("verification")
    if not isinstance(policy, dict):
        raise HarnessError("qualified check requires a frozen verification contract")
    if config["workload_ref"].get("verification_engine_sha256") != engine_digest():
        raise HarnessError("verification engine changed after run qualification; start a new run")
    attempt = hashlib.sha256(
        f"{evidence_key(requirement)}:{secrets.token_hex(16)}".encode()
    ).hexdigest()
    started_at = time.time()
    begin_attempt(root, requirement, attempt)
    with tempfile.TemporaryDirectory(prefix="qualified-check-", dir=config["run_dir"]) as raw:
        temporary = Path(raw)
        stdout = temporary / "stdout.log"
        stderr = temporary / "stderr.log"
        declared = temporary / "declared"
        declared.mkdir()
        environment = os.environ.copy()
        environment.update(
            {str(key): str(value) for key, value in check.get("environment", {}).items()}
        )
        environment.update(
            {
                "OXIDE_FROZEN_CONTRACT_ROOT": str(config["contract_root"]),
                "OXIDE_CANDIDATE_COMMIT": candidate_commit,
                "OXIDE_CANDIDATE_TREE": candidate_tree,
                "OXIDE_PROSPECTIVE_COMMIT": prospective_commit,
                "OXIDE_PROSPECTIVE_TREE": prospective_tree,
                "OXIDE_EVIDENCE_RECEIPT": str(declared / "receipt.json"),
                "OXIDE_EVIDENCE_ARTIFACT_DIR": str(declared / "artifacts"),
            }
        )
        exit_code: int | None = None
        result_kind = "infrastructure_failure"
        try:
            expected_environment = policy.get("execution_environment")
            if expected_environment is not None and expected_environment != observed_environment():
                raise EvidenceError("execution environment differs from contract qualification")
            if check.get("driver") == "verus":
                command = invocation(
                    repository,
                    config["contract_root"],
                    str(check["operation"]),
                    contract_path=str(config["workload_ref"]["contract_path"]),
                    root=check.get("root"),
                    candidate_tree=candidate_tree,
                    prospective_tree=prospective_tree,
                    receipt=declared / "receipt.json",
                    artifact_dir=declared / "artifacts",
                )
            elif check.get("driver") == "command":
                command = [COMMAND_SHELL, "-lc", "set -e\n" + str(check["command"])]
            else:
                raise EvidenceError("qualified check has an unsupported driver")
            working_directory = (repository / str(check.get("working_directory", "."))).resolve()
            if not working_directory.is_relative_to(repository) or not working_directory.is_dir():
                raise EvidenceError("qualified check working directory is unavailable")
            with stdout.open("wb") as out, stderr.open("wb") as err:
                completed = subprocess.run(
                    command,
                    cwd=repository if check.get("driver") == "verus" else working_directory,
                    env=environment,
                    stdout=out,
                    stderr=err,
                    timeout=int(policy["timeout_seconds"]),
                    check=False,
                )
            exit_code = completed.returncode
            result_kind = (
                "passed"
                if exit_code == 0
                else "infrastructure_failure"
                if exit_code in policy["infrastructure_exit_codes"]
                else "product_failure"
            )
            if receipt_required:
                validate_declared_json_receipt(
                    declared / "receipt.json",
                    maximum_bytes=int(policy["max_artifact_bytes"]),
                )
        except subprocess.TimeoutExpired:
            stderr.write_text("qualified command timed out\n", encoding="utf-8")
            stdout.touch(exist_ok=True)
            exit_code = 124
        except (OSError, EvidenceError) as error:
            with stderr.open("a", encoding="utf-8") as stream:
                stream.write(f"qualified command infrastructure failure: {error}\n")
            stdout.touch(exist_ok=True)
            exit_code = 2
            result_kind = "infrastructure_failure"
        artifacts = [path for path in declared.rglob("*") if path.is_file()]
        receipt, digest = finish_attempt(
            root,
            requirement,
            attempt,
            result=result_kind,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            artifact_paths=artifacts,
            maximum_artifact_bytes=int(policy["max_artifact_bytes"]),
            started_at=started_at,
        )
    return receipt, digest


def _qualify_contract(config: dict[str, Any], stage: dict[str, Any]) -> None:
    verification = stage["verification"]
    policy_ref = config["workload_ref"]["verification"]
    target = Path(config["target_repo"])
    with tempfile.TemporaryDirectory(
        prefix="contract-qualification-", dir=config["run_dir"]
    ) as raw:
        repository = Path(raw) / "repository"
        completed = subprocess.run(
            ["git", "clone", "--no-hardlinks", str(target), str(repository)],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            raise HarnessError(
                completed.stderr.strip() or "cannot create contract qualification clone"
            )
        _git(repository, "checkout", "--detach", str(config["base_commit"]))
        tree = _git(repository, "rev-parse", "HEAD^{tree}")
        requirement = {
            "schema": "OxideVerificationQualificationV1",
            "run_id": config["run_id"],
            "base_commit": config["base_commit"],
            "base_tree": tree,
            "contract_path": config["workload_ref"]["contract_path"],
            "contract_blob": config["workload_ref"]["contract_blob"],
            "contract_closure": config["workload_ref"]["contract_closure_sha256"],
            "verification_engine": config["workload_ref"]["verification_engine_sha256"],
            "operations": list(verification["qualification"]),
            "environment": observed_environment(),
        }
        digests: list[str] = []
        for operation in verification["qualification"]:
            operation_requirement = {**requirement, "operation": operation}
            receipt, digest = _run_qualified_check(
                config,
                repository,
                {"driver": "verus", "operation": operation},
                operation_requirement,
                candidate_commit=str(config["base_commit"]),
                candidate_tree=tree,
                prospective_commit=str(config["base_commit"]),
                prospective_tree=tree,
                receipt_required=False,
            )
            if receipt["result"] != "passed":
                raise HarnessError(
                    f"frozen contract qualification failed ({operation}): {receipt['result']}"
                )
            digests.append(digest)
    policy_ref["qualification_receipt_sha256"] = sha256_bytes(canonical_bytes(digests))
    policy_ref["execution_environment"] = observed_environment()


def _merge_failed(
    config: dict[str, Any],
    client: WorkflowClient,
    task: dict[str, Any],
    reason: str,
    *,
    kind: str = "product",
) -> None:
    client.add(
        config["run_id"],
        "launcher",
        "\n".join(
            (
                "control: merge-failed",
                f"task: {task['root_task_id']}",
                f"generation: {task['generation']}",
                f"head: {task['head_sha']}",
                f"kind: {kind}",
                f"reason: {' '.join(reason.splitlines())[:1800]}",
            )
        ),
    )


def _merge_task(
    config: dict[str, Any],
    client: WorkflowClient,
    task: dict[str, Any],
    log: Callable[[str], None],
) -> None:
    target = Path(config["target_repo"])
    target_branch = str(config["target_branch"])
    branch = str(task["branch"])
    head = str(task["head_sha"])
    if _git(target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) != target_branch:
        raise HarnessError("cannot merge: target is not on its staged branch")
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise HarnessError("cannot merge over target changes")
    if _git(target, "rev-parse", "--verify", f"refs/heads/{branch}", check=False) != head:
        _merge_failed(config, client, task, "PR branch no longer matches approved head")
        return
    if _git(target, "rev-parse", f"{head}^{{tree}}", check=False) != task.get("tree_sha"):
        _merge_failed(
            config, client, task, "candidate commit no longer resolves to its frozen tree"
        )
        return
    before = _git(target, "rev-parse", "HEAD")
    with tempfile.TemporaryDirectory(prefix="merge-check-", dir=config["run_dir"]) as temporary:
        verification = Path(temporary) / "repository"
        cloned = subprocess.run(
            ["git", "clone", "--no-hardlinks", str(target), str(verification)],
            text=True,
            capture_output=True,
            check=False,
        )
        if cloned.returncode:
            raise HarnessError(cloned.stderr.strip() or "could not create merge-check clone")
        _git(verification, "checkout", target_branch)
        candidate = f"refs/remotes/origin/{branch}"
        if _git(verification, "rev-parse", "--verify", candidate, check=False) != head:
            _merge_failed(config, client, task, "merge-check clone saw a different PR head")
            return
        if not _git_succeeds(verification, "merge-base", "--is-ancestor", head, "HEAD"):
            identity = dict(config["git_identity"])
            prospective = subprocess.run(
                [
                    "git",
                    "-C",
                    str(verification),
                    "-c",
                    f"user.name={identity['name']}",
                    "-c",
                    f"user.email={identity['email']}",
                    "merge",
                    "--no-ff",
                    "--no-edit",
                    candidate,
                ],
                text=True,
                capture_output=True,
                check=False,
            )
            if prospective.returncode:
                _merge_failed(
                    config,
                    client,
                    task,
                    prospective.stderr.strip() or "candidate conflicts with current target",
                )
                return
        verification_ref = config["workload_ref"]["verification"]
        protected = sorted(set(config["workload_ref"]["immutable_paths"]))
        if not _git_succeeds(verification, "diff", "--quiet", before, "HEAD", "--", *protected):
            _merge_failed(
                config,
                client,
                task,
                "candidate modifies immutable verification-contract inputs",
            )
            return
        verified_commit = _git(verification, "rev-parse", "HEAD")
        expected_tree = _git(verification, "rev-parse", "HEAD^{tree}")
        prospective_receipt = ""
        stage = _load_frozen_stage(config)
        verification_contract = stage["verification"]
        if isinstance(verification_contract, dict):
            operation = str(verification_contract["prospective_operation"])
            requirement = {
                "schema": "OxideProspectiveGateRequirementV1",
                "run_id": config["run_id"],
                "epoch": int(config["epoch"]),
                "workload": {
                    "base_commit": config["workload_ref"]["base_commit"],
                    "contract_path": config["workload_ref"]["contract_path"],
                    "contract_blob": config["workload_ref"]["contract_blob"],
                    "contract_closure": config["workload_ref"]["contract_closure_sha256"],
                    "verification_engine": config["workload_ref"]["verification_engine_sha256"],
                },
                "candidate": {
                    "task": task["root_task_id"],
                    "generation": int(task["generation"]),
                    "base": task["base_sha"],
                    "commit": head,
                    "tree": task["tree_sha"],
                },
                "prospective": {
                    "base": before,
                    "commit": verified_commit,
                    "tree": expected_tree,
                },
                "check": {
                    "id": "prospective-authoritative-tree",
                    "driver": "verus",
                    "operation": operation,
                    "working_directory": ".",
                    "receipt_required": verification_ref["prospective_receipt_required"],
                },
                "qualification": {
                    "contract_closure": config["workload_ref"]["contract_closure_sha256"],
                    "verification_engine": config["workload_ref"]["verification_engine_sha256"],
                    "receipt": verification_ref["qualification_receipt_sha256"],
                    "environment": verification_ref["execution_environment"],
                    "policy": verification_ref["evidence_policy"],
                },
            }
            receipt, prospective_receipt = _run_qualified_check(
                config,
                verification,
                {"driver": "verus", "operation": operation},
                requirement,
                candidate_commit=head,
                candidate_tree=str(task["tree_sha"]),
                prospective_commit=verified_commit,
                prospective_tree=expected_tree,
                receipt_required=verification_ref["prospective_receipt_required"],
            )
            if receipt["result"] != "passed":
                _merge_failed(
                    config,
                    client,
                    task,
                    f"prospective-tree gate {receipt['result']}",
                    kind=(
                        "infrastructure"
                        if receipt["result"] == "infrastructure_failure"
                        else "product"
                    ),
                )
                return
        transferred = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "fetch",
                "--no-tags",
                str(verification),
                verified_commit,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if transferred.returncode:
            raise HarnessError(
                transferred.stderr.strip() or "could not import the verified merge object"
            )
    if _git(target, "rev-parse", "HEAD") != before:
        raise HarnessError("target changed during prospective merge verification")
    if _git(target, "rev-parse", f"refs/heads/{branch}") != head:
        raise HarnessError("PR head changed during prospective merge verification")
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise HarnessError("target changed during prospective merge verification")
    if verified_commit != before:
        actual = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "merge",
                "--ff-only",
                "--no-edit",
                verified_commit,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if actual.returncode:
            _git(target, "merge", "--abort", check=False)
            _merge_failed(
                config,
                client,
                task,
                actual.stderr.strip() or "verified merge fast-forward failed",
            )
            return
    merged = _git(target, "rev-parse", "HEAD")
    tree = _git(target, "rev-parse", "HEAD^{tree}")
    if merged != verified_commit or tree != expected_tree:
        raise HarnessError("target does not equal the exact prospective verified commit")
    client.add(
        config["run_id"],
        "launcher",
        "\n".join(
            (
                "control: merged",
                f"task: {task['root_task_id']}",
                f"generation: {task['generation']}",
                f"head: {head}",
                f"merge: {merged}",
                f"tree: {tree}",
                f"candidate-tree: {task['tree_sha']}",
                f"prospective-receipt: {prospective_receipt}",
            )
        ),
    )
    log(f"merged {task['root_task_id']} PR#{task['generation']} at {merged[:12]}")


def _publish(config: dict[str, Any], client: WorkflowClient, log: Callable[[str], None]) -> str:
    target = Path(config["target_repo"])
    target_branch = str(config["target_branch"])
    if _git(target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) != target_branch:
        raise HarnessError("cannot publish: target is not on its staged branch")
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise HarnessError("cannot publish over target changes")
    tip = _git(target, "rev-parse", "HEAD")
    tasks = client.search(config["run_id"], "queue:all")
    if not tasks or any(task.get("state") != "complete" for task in tasks):
        raise HarnessError("cannot publish before every reviewed PR is merged")
    for task in tasks:
        commit = str(task.get("merged_sha") or "")
        if not re.fullmatch(r"[0-9a-f]{40}", commit) or not _git_succeeds(
            target, "merge-base", "--is-ancestor", commit, tip
        ):
            raise HarnessError(f"task {task['task_id']} merge is absent from main")
    result = client.add(
        config["run_id"],
        "launcher",
        f"control: published\ncommit: {tip}",
    )
    if result.get("state") != "complete":
        raise HarnessError("workflow did not accept publication")
    return tip


def _terminal_command(arguments: list[str]) -> str:
    return f"cd {shlex.quote(str(ROOT))} && exec {shlex.join(arguments)}"


def _launch_terminal(arguments: list[str]) -> None:
    subprocess.Popen(
        arguments,
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _wait_socket(path: Path, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise HarnessError(f"journal socket did not appear: {path}")
        time.sleep(0.1)


def _using_journal(config: dict[str, Any], call: Callable[[WorkflowClient], Any]) -> Any:
    socket_path = Path(config["socket"])
    try:
        return call(_workflow_client(config, connect_journal(socket_path)))
    except (ConnectionRefusedError, FileNotFoundError):
        runtime = start_journal(
            config["database"],
            socket_path,
            config.get("journal_command") or None,
            min_exact=int(config["min_exact"]),
            max_results=int(config["max_results"]),
        )
    try:
        return call(_workflow_client(config, connect_journal(socket_path)))
    finally:
        runtime.close()


_OBSERVER_TASKS: dict[tuple[str, str], str] = {}


def _observer_context(config: dict[str, Any], slot: str) -> tuple[str, tuple[str, str]]:
    key = (str(config["run_id"]), slot)
    assignment = Path(config["run_dir"]) / "assignments" / f"{slot}.txt"
    assigned_role = (
        assignment.read_text(encoding="utf-8").splitlines()[0] if assignment.exists() else ""
    )
    status = (
        str(config.get("model") or "gpt 5.6 sol medium"),
        assigned_role or _OBSERVER_TASKS.get(key, "-"),
    )
    try:
        client = _workflow_client(config, connect_journal(config["socket"]))
        view = client._view(config["run_id"])
        matches = client.reducer.search(view, f"worker:{slot}")
        if matches:
            role = str(matches[0]["role"])
            role = "implementation" if role in {"author", "revision"} else role
            _OBSERVER_TASKS[key] = role
            status = (status[0], role)
        return view.state, status
    except (JournalError, WorkflowError, json.JSONDecodeError, OSError, IndexError, KeyError):
        return "starting", status


def _process_table() -> list[tuple[int, str]]:
    result = subprocess.run(
        ["ps", "-axo", "pid=,command=", "-ww"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise HarnessError(result.stderr.strip() or "could not inspect processes")
    rows: list[tuple[int, str]] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2 and parts[0].isdigit():
            rows.append((int(parts[0]), parts[1]))
    return rows


def _run_processes(config: dict[str, Any]) -> list[tuple[int, str]]:
    workload = str(config["workload"])
    oxide = str(ROOT / "oxide")
    worker_root = str((Path(config["run_dir"]) / "workers").resolve())
    workload_argument = re.compile(
        r"(?:^|\s)--workload(?:=|\s+)" + re.escape(workload) + r"(?:\s|$)"
    )
    rows: list[tuple[int, str]] = []
    for pid, command in _process_table():
        belongs_to_run = oxide in command and workload_argument.search(command) is not None
        if belongs_to_run and "harness worker " in command:
            rows.append((pid, "worker"))
        elif belongs_to_run and "harness launch " in command:
            rows.append((pid, "launcher"))
        elif belongs_to_run and (
            "harness observe " in command or "harness observe-queue " in command
        ):
            rows.append((pid, "observer"))
        elif "codex exec" in command and worker_root in command:
            rows.append((pid, "codex"))
    return rows


def _live_slots(config: dict[str, Any]) -> set[str]:
    pattern = re.compile(
        re.escape(str(ROOT / "oxide"))
        + r" harness worker --workload "
        + re.escape(str(config["workload"]))
        + r" --slot ([^ ]+)"
    )
    return {
        match.group(1)
        for _, command in _process_table()
        if (match := pattern.search(command)) is not None
    }


def _worker_argv(workload: str, slot: str) -> list[str]:
    return [str(ROOT / "oxide"), "harness", "worker", "--workload", workload, "--slot", slot]


class _Supervisor:
    def __init__(self, config, client, log) -> None:
        self.config = config
        self.client = client
        self.log = log
        self.expected = {f"worker-{index}" for index in range(int(config["workers"]))}
        self.starting: dict[str, float] = {}

    def _launch(self, slot: str) -> None:
        _launch_terminal(_worker_argv(str(self.config["workload"]), slot))
        self.starting[slot] = time.monotonic() + 5
        self.log(f"launched {slot}")

    def tick(self) -> None:
        live = _live_slots(self.config) & self.expected
        now = time.monotonic()
        for slot in live:
            self.starting.pop(slot, None)
        starting = {slot for slot, deadline in self.starting.items() if now < deadline}
        unavailable = self.expected - live - starting
        for item in self.client.search(self.config["run_id"], "queue:all"):
            owner = item.get("worker_id")
            if item.get("state") == "working" and owner in unavailable:
                result = self.client.add(
                    self.config["run_id"], "launcher", f"control: reclaim worker:{owner}"
                )
                if result["saved"]:
                    self.log(f"reclaimed {result['reclaimed']} from crashed {owner}")
        for slot in sorted(unavailable):
            self._launch(slot)


def _signal(pid: int, kind: str, signal_number: int) -> None:
    try:
        os.killpg(pid, signal_number) if kind == "codex" else os.kill(pid, signal_number)
    except ProcessLookupError:
        pass


def _stop_processes(config: dict[str, Any]) -> None:
    for signal_number, timeout in (
        (signal.SIGINT, 10),
        (signal.SIGTERM, 5),
        (signal.SIGKILL, 2),
    ):
        rows = _run_processes(config)
        if not rows:
            return
        for pid, kind in rows:
            _signal(pid, kind, signal_number)
        deadline = time.monotonic() + timeout
        while _run_processes(config) and time.monotonic() < deadline:
            time.sleep(0.1)
    remaining = _run_processes(config)
    if remaining:
        raise HarnessError(f"run processes did not stop: {remaining}")


def _start(config: dict[str, Any], foreground: bool) -> int:
    if foreground:
        return command_launch(argparse.Namespace(workload=config["workload"]))
    _launch_terminal([str(ROOT / "oxide"), "harness", "launch", "--workload", config["workload"]])
    print(f"Started {config['workload']} in the background.")
    print(f"Observe: ./oxide harness observe --workload {config['workload']} --slot worker-0")
    print(f"Queue:   ./oxide harness observe-queue --workload {config['workload']}")
    return 0


def _validate_bound_concurrency(config: dict[str, Any]) -> None:
    bound = config.get("concurrency_validation")
    if not isinstance(bound, dict):
        raise ConcurrencyError("workload run has no bound concurrency receipt")
    receipt = validate_receipt(
        ROOT,
        Path(str(bound.get("report_path", ""))),
        required_workers=int(config["workers"]),
        require_current_source=True,
        journal_command=config.get("journal_command") or None,
        min_exact=int(config["min_exact"]),
        max_results=int(config["max_results"]),
    )
    fields = (
        "source_digest",
        "kernel_digest",
        "workers",
        "rounds",
        "seed",
        "min_exact",
        "max_results",
        "report_path",
    )
    if any(bound.get(name) != receipt.get(name) for name in fields):
        raise ConcurrencyError("workload run does not match its concurrency receipt")


def command_run(arguments: argparse.Namespace) -> int:
    target = Path(arguments.target).expanduser().resolve()
    if arguments.workers < 1:
        raise HarnessError("workers must be positive")
    try:
        min_exact, max_results = validate_search_capacity(
            int(arguments.min_exact), int(arguments.max_results)
        )
    except JournalError as error:
        raise HarnessError(str(error)) from error
    if (
        not target.is_dir()
        or _git(target, "rev-parse", "--is-inside-work-tree", check=False) != "true"
    ):
        raise HarnessError("target must be a Git worktree")
    if Path(_git(target, "rev-parse", "--show-toplevel")).resolve() != target:
        raise HarnessError("target must be the Git worktree root")
    contract_path = _contract_path(target, arguments.contract)
    try:
        stage = load_contract(contract_path)
    except ContractError as error:
        raise HarnessError(str(error)) from error
    workload = str(stage["contract"]["id"])
    arguments.workload = workload
    run_dir = _run_dir(workload)
    if _config_path(workload).exists():
        raise HarnessError("run already exists; use resume or reset")
    required_reviews = (
        int(arguments.reviews) if arguments.reviews is not None else int(stage["minimum_reviews"])
    )
    if not int(stage["minimum_reviews"]) <= required_reviews <= 16:
        raise HarnessError(
            f"reviews must be between the contract minimum ({stage['minimum_reviews']}) and 16"
        )
    contract_relative = str(contract_path.relative_to(target))
    if not _git_succeeds(target, "ls-files", "--error-unmatch", "--", contract_relative):
        raise HarnessError(
            f"verification contract must be committed under verification/: {contract_relative}"
        )
    journal_command = shlex.split(getattr(arguments, "journal_command", "") or "")
    receipt_path = Path(
        getattr(arguments, "concurrency_receipt", None)
        or ROOT / ".oxide" / "validation" / "latest.json"
    ).expanduser()
    concurrency_receipt = validate_receipt(
        ROOT,
        receipt_path,
        required_workers=arguments.workers,
        journal_command=journal_command or None,
        min_exact=min_exact,
        max_results=max_results,
    )
    target_branch = _git(target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if not target_branch:
        raise HarnessError("target must have a checked-out branch")
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise HarnessError("target has changes; commit, stash, or remove them first")
    base_commit = _git(target, "rev-parse", "HEAD")
    git_identity = _target_git_identity(target)
    run_id = f"{workload}-{time.strftime('%Y%m%d-%H%M%S')}"
    workload_ref = _frozen_workload_ref(target, base_commit, contract_path, stage)
    config = {
        "schema_version": 9,
        "run_id": run_id,
        "workload": workload,
        "contract_path": str(contract_path),
        "target_repo": str(target),
        "target_branch": target_branch,
        "base_commit": base_commit,
        "git_identity": git_identity,
        "run_dir": str(run_dir),
        "database": str(run_dir / "journal.sqlite3"),
        "socket": str(run_dir / "journal.sock"),
        "evidence_root": str(run_dir / "evidence" / "checks"),
        "contract_root": str(run_dir / "frozen-contract"),
        "workers": arguments.workers,
        "required_reviews": required_reviews,
        "model": arguments.model,
        "journal_command": journal_command,
        "min_exact": min_exact,
        "max_results": max_results,
        "epoch": 0,
        "history_sequence": 0,
        "epoch_frontiers": [],
        "replay_root": secrets.token_hex(16),
        "workload_ref": workload_ref,
        "harness_version": workload_ref["harness_version"],
        "branch_prefix": f"codex/oxide-{_slug(run_id)}",
        "stage": stage["stage"],
    }
    config["concurrency_validation"] = {
        "source_digest": concurrency_receipt["source_digest"],
        "kernel_digest": concurrency_receipt["kernel_digest"],
        "workers": concurrency_receipt["workers"],
        "rounds": concurrency_receipt["rounds"],
        "seed": concurrency_receipt["seed"],
        "min_exact": concurrency_receipt["min_exact"],
        "max_results": concurrency_receipt["max_results"],
        "report_path": concurrency_receipt["report_path"],
    }
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        Path(config["evidence_root"]).mkdir(parents=True)
        _materialize_contract(config)
        _qualify_contract(config, stage)
        _atomic_json(_config_path(workload), config)
        _snapshot_checkpoint(config, "initial")
    except Exception:
        shutil.rmtree(run_dir, ignore_errors=True)
        raise
    return _start(config, arguments.foreground)


def command_launch(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    _validate_bound_concurrency(config)
    log = _Log(Path(config["run_dir"]) / "logs" / "orchestrator.log")
    runtime = start_journal(
        config["database"],
        config["socket"],
        config.get("journal_command") or None,
        min_exact=int(config["min_exact"]),
        max_results=int(config["max_results"]),
    )
    client = _workflow_client(config, runtime.client)
    try:
        result = client.bootstrap(config["run_id"])
        log(f"run {config['run_id']}: {'created' if result['saved'] else 'resumed'}")
        state = str(result["state"])
        capacity_record = (
            f"control: worker-capacity\nworkers: {int(config['workers'])}\nterminal-blockers: true"
        )
        if state == "running" and not any(
            item.get("body") == capacity_record
            for item in client.search(config["run_id"], "control: worker-capacity")
        ):
            client.add(config["run_id"], "launcher", capacity_record)
        supervisor: _Supervisor | None = None
        if state == "running":
            _prepare_repositories(config)
            supervisor = _Supervisor(config, client, log)
        elif state != "publishing":
            log(f"run {config['run_id']}: {state.upper()}")
            return 0 if state in {"paused", "complete", "stopped"} else 1
        while True:
            try:
                state = str(client.search(config["run_id"], "run:state")[0]["state"])
                if state == "publishing":
                    tip = _publish(config, client, log)
                    log(f"verified reviewed main {tip[:12]} on {config['target_branch']}")
                    log(f"run {config['run_id']}: COMPLETE")
                    return 0
                if state != "running":
                    log(f"run {config['run_id']}: {state.upper()}")
                    return 0 if state in {"paused", "complete", "stopped"} else 1
                assert supervisor is not None
                for task in client.search(config["run_id"], "merge:requested"):
                    _merge_task(config, client, task, log)
                supervisor.tick()
                time.sleep(0.5)
            except JournalTimeoutError as error:
                log(f"journal operation will retry: {error}")
                time.sleep(1)
    except Exception as error:
        log(f"launcher failed: {type(error).__name__}: {error}")
        raise
    finally:
        runtime.close()


def command_worker(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    _validate_bound_concurrency(config)
    _wait_socket(Path(config["socket"]))
    log = _Log(Path(config["run_dir"]) / "logs" / f"{arguments.slot}.log")
    worker = Worker(
        _workflow_client(config, connect_journal(config["socket"])),
        config["run_id"],
        arguments.slot,
        Path(config["run_dir"]) / "workers" / arguments.slot,
        config["target_branch"],
        config["target_repo"],
        journal_socket=config["socket"],
        model=config.get("model"),
        assignment_path=Path(config["run_dir"]) / "assignments" / f"{arguments.slot}.txt",
        run_config=_config_path(arguments.workload),
        evidence_root=config["evidence_root"],
        contract_root=config["contract_root"],
        contract_path=str(config["workload_ref"]["contract_path"]),
        epoch=int(config["epoch"]),
        log=log,
    )

    def stop_worker(signum: int, _frame: Any) -> None:
        worker.stop()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stop_worker)
    signal.signal(signal.SIGINT, stop_worker)
    state = worker.run()
    log(f"slot stopped: {state}")
    return 0 if state in {"paused", "publishing", "complete", "stopped"} else 1


def command_pause(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    state = str(
        _using_journal(
            config, lambda client: client.search(config["run_id"], "run:state")[0]["state"]
        )
    )
    if state not in {"complete", "failed"}:
        result = _using_journal(
            config,
            lambda client: client.add(config["run_id"], "launcher", "control: pause"),
        )
        state = result["state"]
    _stop_processes(config)
    print(f"{arguments.workload}: {state}")
    return 0


def command_resume(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    _validate_bound_concurrency(config)
    if any(kind == "launcher" for _, kind in _run_processes(config)):
        raise HarnessError("workload already has a live launcher")
    _using_journal(
        config,
        lambda client: client.add(config["run_id"], "launcher", "control: resume"),
    )
    return _start(config, arguments.foreground)


def command_reset(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    command_pause(argparse.Namespace(workload=arguments.workload))
    target = Path(config["target_repo"])
    refs = _git(
        target,
        "for-each-ref",
        "--format=%(refname)",
        f"refs/heads/{config['branch_prefix']}/",
    ).splitlines()
    for ref in refs:
        _git(target, "update-ref", "-d", ref)
    run_dir = Path(config["run_dir"])
    archive = RUNS.parent / "archive" / f"{arguments.workload}-{config['run_id']}"
    archive.parent.mkdir(parents=True, exist_ok=True)
    if archive.exists():
        archive = archive.with_name(archive.name + f"-{time.time_ns()}")
    run_dir.replace(archive)
    print(f"Reset {arguments.workload}; archived prior state at {archive}")
    return 0


def _checkpoint_path(config: dict[str, Any], name: str) -> Path:
    if _WORKLOAD_NAME.fullmatch(name) is None:
        raise HarnessError("checkpoint must be a safe name")
    return CHECKPOINTS / str(config["workload"]) / str(config["run_id"]) / name


def _validate_checkpoint_manifest(config: dict[str, Any], manifest: object) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise HarnessError("checkpoint manifest is invalid")
    refs = manifest.get("task_refs")
    ref_prefix = f"refs/heads/{config['branch_prefix']}/"
    commit = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
    if (
        manifest.get("schema") != "OxideDestructiveCheckpointV1"
        or manifest.get("run_id") != config["run_id"]
        or manifest.get("workload") != config["workload"]
        or manifest.get("target_repository") != config["workload_ref"]["target_repository"]
        or manifest.get("target_branch") != config["target_branch"]
        or commit.fullmatch(str(manifest.get("target_head", ""))) is None
        or not isinstance(manifest.get("source_epoch"), int)
        or int(manifest["source_epoch"]) < 0
        or not isinstance(manifest.get("journal_sequence"), int)
        or int(manifest["journal_sequence"]) < 0
        or not isinstance(refs, dict)
        or any(
            not isinstance(ref, str)
            or not ref.startswith(ref_prefix)
            or commit.fullmatch(str(value)) is None
            for ref, value in refs.items()
        )
    ):
        raise HarnessError("checkpoint does not belong to this run or is malformed")
    return manifest


def _journal_sequence(config: dict[str, Any]) -> int:
    database = Path(config["database"])
    if not database.exists():
        return 0
    runtime = start_journal(
        database,
        config["socket"],
        config.get("journal_command") or None,
        min_exact=int(config["min_exact"]),
        max_results=int(config["max_results"]),
    )
    try:
        records = _workflow_client(config, runtime.client).replay_records(config["run_id"])
        return int(records[-1]["journal_sequence"]) if records else 0
    finally:
        runtime.close()


def _snapshot_checkpoint(config: dict[str, Any], name: str) -> Path:
    destination = _checkpoint_path(config, name)
    if destination.exists():
        raise HarnessError(f"checkpoint already exists: {name}")
    target = Path(config["target_repo"])
    if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
        raise HarnessError("target must be clean before creating a checkpoint")
    run_dir = Path(config["run_dir"])
    temporary = destination.with_name(destination.name + f".tmp-{os.getpid()}")
    temporary.parent.mkdir(parents=True, exist_ok=True)
    if temporary.exists():
        shutil.rmtree(temporary)
    state = temporary / "run-state"
    shutil.copytree(
        run_dir,
        state,
        ignore=shutil.ignore_patterns("journal.sock", "workflow.lock"),
    )
    refs = {}
    for line in _git(
        target,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        f"refs/heads/{config['branch_prefix']}/",
    ).splitlines():
        ref, commit = line.split(maxsplit=1)
        refs[ref] = commit
    manifest = {
        "schema": "OxideDestructiveCheckpointV1",
        "run_id": config["run_id"],
        "workload": config["workload"],
        "source_epoch": int(config["epoch"]),
        "journal_sequence": _journal_sequence(config),
        "target_repository": config["workload_ref"]["target_repository"],
        "target_branch": config["target_branch"],
        "target_head": _git(target, "rev-parse", config["target_branch"]),
        "task_refs": refs,
        "created_at": time.time(),
    }
    _atomic_json(temporary / "manifest.json", manifest)
    temporary.replace(destination)
    return destination


def command_checkpoint(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    command_pause(argparse.Namespace(workload=arguments.workload))
    with _exclusive_run(arguments.workload):
        config = _load_config(arguments.workload)
        if _run_processes(config):
            raise HarnessError("run must be fully stopped before checkpointing")
        destination = _snapshot_checkpoint(config, arguments.name)
    print(f"Checkpoint {arguments.name} created at {destination}")
    return 0


def command_rewind(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    checkpoint = _checkpoint_path(config, arguments.to)
    try:
        manifest = _validate_checkpoint_manifest(
            config,
            json.loads((checkpoint / "manifest.json").read_text(encoding="utf-8")),
        )
    except (OSError, json.JSONDecodeError) as error:
        raise HarnessError(f"checkpoint is unavailable: {arguments.to}") from error
    try:
        command_pause(argparse.Namespace(workload=arguments.workload))
    except (HarnessError, JournalError, WorkflowError, ConnectionError, OSError):
        _stop_processes(config)
    with _exclusive_run(arguments.workload):
        # Another administrative command may have completed while this rewind
        # waited for exclusivity. Epoch advancement must use the current external
        # run state, not the pre-pause snapshot held by this process.
        config = _load_config(arguments.workload)
        manifest = _validate_checkpoint_manifest(config, manifest)
        _stop_processes(config)
        target = Path(config["target_repo"])
        if config["workload_ref"]["target_repository"] != _repository_identity(target):
            raise HarnessError("checkpoint target repository identity does not match")
        if (
            _git(target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
            != config["target_branch"]
        ):
            raise HarnessError("rewind requires the target's staged branch to be checked out")
        if _git(target, "status", "--porcelain=v1", "--untracked-files=all"):
            raise HarnessError("rewind refuses to overwrite target working tree changes")
        run_dir = Path(config["run_dir"])
        if arguments.archive:
            archive = (
                ROOT
                / ".oxide"
                / "rewind-archive"
                / f"{config['run_id']}-epoch-{config['epoch']}-{time.time_ns()}"
            )
            archive.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(run_dir, archive, ignore=shutil.ignore_patterns("journal.sock"))
        restored = run_dir.with_name(run_dir.name + f".rewind-{os.getpid()}")
        if restored.exists():
            shutil.rmtree(restored)
        shutil.copytree(checkpoint / "run-state", restored)
        try:
            restored_config = json.loads((restored / "run.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HarnessError("checkpoint run state is invalid") from error
        if (
            not isinstance(restored_config, dict)
            or restored_config.get("run_id") != config["run_id"]
            or restored_config.get("workload") != config["workload"]
            or restored_config.get("epoch") != manifest["source_epoch"]
            or not _valid_epoch_frontiers(
                restored_config.get("epoch_frontiers"),
                restored_config.get("epoch"),
                restored_config.get("history_sequence"),
            )
        ):
            raise HarnessError("checkpoint run state is malformed")
        frontiers = [dict(item) for item in restored_config["epoch_frontiers"]]
        prior_sequence = int(frontiers[-1]["through"]) if frontiers else 0
        checkpoint_sequence = int(manifest["journal_sequence"])
        if checkpoint_sequence < prior_sequence:
            raise HarnessError("checkpoint epoch frontier is inconsistent")
        if checkpoint_sequence > prior_sequence:
            frontiers.append(
                {
                    "epoch": int(manifest["source_epoch"]),
                    "through": checkpoint_sequence,
                }
            )
        current_refs = _git(
            target,
            "for-each-ref",
            "--format=%(refname)",
            f"refs/heads/{config['branch_prefix']}/",
        ).splitlines()
        for ref in current_refs:
            _git(target, "update-ref", "-d", ref)
        for ref, commit in dict(manifest["task_refs"]).items():
            _git(target, "update-ref", str(ref), str(commit))
        _git(target, "reset", "--hard", str(manifest["target_head"]))
        shutil.rmtree(run_dir)
        restored.replace(run_dir)
        restored_config.update(
            epoch=int(config["epoch"]) + 1,
            history_sequence=checkpoint_sequence,
            epoch_frontiers=frontiers,
            run_dir=str(run_dir),
            database=str(run_dir / "journal.sqlite3"),
            socket=str(run_dir / "journal.sock"),
        )
        _atomic_json(run_dir / "run.json", restored_config)
        (run_dir / "journal.sock").unlink(missing_ok=True)
        (run_dir / "workflow.lock").write_text(
            json.dumps(
                {
                    "epoch": restored_config["epoch"],
                    "sequence": restored_config["history_sequence"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        assignments = run_dir / "assignments"
        if assignments.exists():
            for assignment in assignments.glob("*.txt"):
                role = assignment.read_text(encoding="utf-8").splitlines()[0]
                assignment.write_text(
                    f"{role}\nepoch:{restored_config['epoch']}\n",
                    encoding="utf-8",
                )
    restored_config = _load_config(arguments.workload)
    if int(manifest["journal_sequence"]) == 0:
        return _start(restored_config, arguments.foreground)
    _using_journal(
        restored_config,
        lambda client: client.add(restored_config["run_id"], "launcher", "control: resume"),
    )
    print(
        f"Rewound {arguments.workload} to {arguments.to}; "
        f"epoch {restored_config['epoch']}, journal sequence {manifest['journal_sequence']}"
    )
    return _start(restored_config, arguments.foreground)


def _style(value: object, code: str, color: bool) -> str:
    text = str(value)
    return f"\033[{code}m{text}\033[0m" if color else text


def _safe(value: object) -> str:
    rendered: list[str] = []
    for character in str(value):
        codepoint = ord(character)
        if character in "\n\t\r":
            rendered.append({"\n": "\n", "\t": "\\t", "\r": "\\r"}[character])
        elif codepoint < 32 or 127 <= codepoint <= 159:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint in {0x2028, 0x2029} or 0xD800 <= codepoint <= 0xDFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def _code(value: object, language: str, color: bool, base: str = "") -> str:
    if not (safe := _safe(value).rstrip("\n")) or not color:
        return safe
    fallback = _style(safe, base, True) if base else safe
    if language.startswith("yaml"):
        safe = _YAML_VALUE.sub(lambda m: m[1] + _style(m[2], "38;5;214", True), safe)
        return _YAML_KEY.sub(lambda m: m[1] + _style(m[2], "94", True) + m[3], safe)
    if not language:
        return fallback
    try:
        lexer = get_lexer_by_name(language)
        rendered = pygments_highlight(safe, lexer, TerminalFormatter()).removesuffix("\n")
        if base:
            marker = f"\x1b[{base}m"
            rendered = marker + rendered.replace("\x1b[39;49;00m", "\x1b[0m" + marker) + "\x1b[0m"
        return rendered.replace("\x1b[33m", "\x1b[38;5;214m")
    except (ClassNotFound, ImportError):
        return fallback


def _indent(value: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in value.splitlines())


_YAML_VALUE = re.compile(r"(^\s*(?:-\s+)?[\w-]+:|^)(.*)$", re.MULTILINE)
_YAML_KEY = re.compile(r"^(\s*(?:-\s+)?)([\w-]+)(:)", re.MULTILINE)
_QUOTED_YAML = re.compile(r'^(\s*(?:(?:-\s+)?[\w-]+:\s+|-\s+))("(?:[^"\\]|\\.)*")$', re.MULTILINE)
_YAML_WORDS = {"null", "true", "false", "yes", "no", "on", "off", "~"}


def _display_yaml(value: object) -> str:
    def replace(match: re.Match[str]) -> str:
        prefix, encoded = match.groups()
        scalar = json.loads(encoded)
        plain = bool(scalar and scalar == scalar.strip() and scalar.isprintable())
        plain = plain and scalar[0].isalnum() and scalar.lower() not in _YAML_WORDS
        plain &= all(marker not in scalar for marker in (": ", " #"))
        if plain:
            return prefix + scalar
        offset = 4 if prefix.lstrip().startswith("- ") and ":" in prefix else 2
        indentation = " " * (len(prefix) - len(prefix.lstrip()) + offset)
        return prefix + "|-\n" + indentation + scalar.replace("\n", "\n" + indentation)

    return _QUOTED_YAML.sub(replace, _safe(value))


def _tool_value(item: dict[str, Any], phase: str, color: bool) -> str:
    server = item.get("server", "")
    tool = item.get("tool", item.get("name", ""))
    heading = f"{_style('TOOL', '1;36', color)} {_style(phase, '2', color)} {_style(f'{server}.{tool}'.strip('.'), '1;34', color)}"
    arguments = item.get("arguments", item.get("input", {}))
    result = item.get("result", item.get("output"))
    if server == "journal" and isinstance(arguments, dict) and "yaml" in arguments:
        input_yaml = _code(_display_yaml(arguments["yaml"]), "yaml-input", color)
        blocks = [heading, f"{_style('input.yaml', '1;34', color)}:\n" + _indent(input_yaml)]
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                output_yaml = _code(_display_yaml(content[0].get("text", "")), "yaml", color)
                blocks.append(f"{_style('output.yaml', '1;34', color)}:\n" + _indent(output_yaml))
        return "\n".join(blocks)
    return heading + "\n" + _indent(json.dumps(arguments, indent=2, sort_keys=True))


_OUTPUT_LEXERS = {
    ".bash": "bash",
    ".css": "css",
    ".html": "html",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "javascript",
    ".md": "markdown",
    ".py": "python",
    ".rs": "rust",
    ".sh": "bash",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".zsh": "bash",
}
_DISPLAY_PATH = re.compile(
    r"""[^\s"';&|()<>]+(?:\.(?:bash|css|html|js|json|jsx|md|py|rs|sh|sql|toml|ts|tsx|yaml|yml|zsh))\b"""
)
_DISPLAY_COMMAND = re.compile(
    r"(?<![\w-])(?:sed|cat|head|tail)\b(?P<arguments>.*?)(?=&&|;|\|\||\n|$)"
)


def _command_output_language(command: object) -> str:
    source = str(command)
    if "git diff" in source or re.search(r"\bgit\s+show\b", source):
        return "diff"
    displays = list(_DISPLAY_COMMAND.finditer(source))
    if not displays:
        return ""
    languages = [
        _OUTPUT_LEXERS[Path(path).suffix.lower()]
        for display in displays
        for path in _DISPLAY_PATH.findall(display.group("arguments"))
        if Path(path).suffix.lower() in _OUTPUT_LEXERS
    ]
    if not languages:
        languages = [
            _OUTPUT_LEXERS[Path(path).suffix.lower()]
            for path in _DISPLAY_PATH.findall(source)
            if Path(path).suffix.lower() in _OUTPUT_LEXERS
        ]
    return languages[0] if languages else ""


def _file_change_value(
    item: dict[str, Any],
    phase: str,
    color: bool,
    repository: Path | None,
) -> str:
    changes = item.get("changes")
    changes = changes if isinstance(changes, list) else []
    paths = [
        str(change["path"]) for change in changes if isinstance(change, dict) and change.get("path")
    ]
    relative_paths = item.get("relative_paths")
    displayed_paths = (
        [str(path) for path in relative_paths] if isinstance(relative_paths, list) else paths
    )
    patch = item.get("patch")
    patch = str(patch) if patch else ""
    if not patch and phase == "COMPLETED" and repository is not None:
        inferred_paths, patch = worktree_diff(repository, paths)
        if inferred_paths:
            displayed_paths = inferred_paths
    heading = f"{_style('FILES', '1;36', color)} {_style(phase, '2', color)}"
    parts = [heading]
    if displayed_paths:
        parts.append(
            f"{_style('changed', '1;34', color)}:\n"
            + _indent("\n".join(f"- {path}" for path in displayed_paths))
        )
    if patch:
        parts.append(
            f"{_style('diff', '1;34', color)}:\n" + _indent(_code(patch, "diff", color, "38;5;250"))
        )
    return "\n".join(parts)


def _event_value(event: Any, color: bool, repository: Path | None = None) -> str:
    if isinstance(event, str):
        return _safe(event)
    if not isinstance(event, dict):
        return _code(json.dumps(event), "json", color, "38;5;250")
    event_type = str(event.get("type", "event"))
    phase = event_type.split(".", 1)[-1].upper()
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") in {"mcp_tool_call", "tool_call"}:
        return _tool_value(item, phase, color)
    if isinstance(item, dict) and item.get("type") == "file_change":
        return _file_change_value(item, phase, color, repository)
    if isinstance(item, dict) and item.get("type") in {"command_execution", "command"}:
        command = item.get("command", item.get("cmd", ""))
        output = item.get("aggregated_output", item.get("output", ""))
        parts = [
            f"{_style('COMMAND', '1;36', color)} {_style(phase, '2', color)}",
            f"{_style('command', '1;34', color)}:\n" + _indent(_code(command, "bash", color)),
        ]
        if output:
            language = _command_output_language(command)
            output_text = _code(output, language, color, "38;5;250")
            parts.append(f"{_style('output', '1;34', color)}:\n" + _indent(output_text))
        return "\n".join(parts)
    if isinstance(item, dict) and item.get("type") in {"reasoning", "agent_message"}:
        label = "REASONING" if item["type"] == "reasoning" else "AGENT"
        message = _code(item.get("text", ""), "", color, "38;5;153")
        return f"{_style(label, '1;32', color)} [{_style(phase, '2', color)}]\n" + _indent(message)
    return _code(json.dumps(event, ensure_ascii=False, indent=2), "json", color, "38;5;250")


def highlight_stream_line(
    line: str,
    *,
    color: bool,
    raw: bool = False,
    repository: Path | None = None,
) -> str:
    if raw:
        return line
    match = re.match(r"^(\[\d{2}:\d{2}:\d{2}\]) (.*)$", line)
    body = match.group(2) if match else line
    try:
        rendered = _event_value(json.loads(body), color, repository)
    except json.JSONDecodeError:
        rendered = _safe(body)
    return rendered


def _observer_color(mode: str) -> bool:
    return mode == "always" or mode == "auto" and sys.stdout.isatty()


def _draw_footer(status: tuple[str, str] | None) -> None:
    columns, rows = shutil.get_terminal_size(fallback=(80, 24))
    text = ""
    if status is not None:
        left = columns // 2
        model = status[0][:left].ljust(left)
        role = status[1][: columns - left].rjust(columns - left)
        text = _style((model + role)[:columns].ljust(columns), "2;37;49", True)
    print(f"\x1b[s\x1b[{rows};1H\x1b[2K{text}\x1b[u", end="", flush=True)


def _scroll_lines(rendered: str, animate: bool, footer: tuple[str, str] | None = None) -> None:
    rows = rendered.splitlines() or [""]
    for index, row in enumerate(rows):
        if animate and index:
            time.sleep(1.0 / (len(rows) - 1))
        footer and _draw_footer(None)
        print(row, flush=True)
        footer and _draw_footer(footer)


def command_observe(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    observed_epoch = int(config.get("epoch", 0))
    allowed = {"orchestrator"} | {f"worker-{i}" for i in range(int(config["workers"]))}
    if arguments.slot not in allowed:
        raise HarnessError(f"unknown slot: {arguments.slot}")
    path = Path(config["run_dir"]) / "logs" / f"{arguments.slot}.log"
    repository = Path(config["run_dir"]) / "workers" / arguments.slot
    color = _observer_color(getattr(arguments, "color", "auto"))
    raw = bool(getattr(arguments, "raw", False))
    no_follow = bool(arguments.no_follow)
    while not path.exists():
        if no_follow:
            return 0
        time.sleep(0.1)
    state, status = _observer_context(config, arguments.slot)
    tui = arguments.slot != "orchestrator" and sys.stdout.isatty() and not no_follow
    next_footer_refresh = time.monotonic() + 0.25
    if tui:
        sys.stdout.write("\x1b[?25l\x1b[?4h")
        _draw_footer(status)
    history_end = path.stat().st_size
    try:
        with path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(0 if no_follow else max(0, history_end - 8_192))
            if stream.tell():
                stream.readline()
            while True:
                line = stream.readline()
                if line:
                    now = time.monotonic()
                    if tui and now >= next_footer_refresh:
                        state, status = _observer_context(config, arguments.slot)
                        next_footer_refresh = now + 0.25
                    rendered = highlight_stream_line(
                        line.rstrip("\n"),
                        color=color,
                        raw=raw,
                        repository=repository,
                    )
                    _scroll_lines(rendered, stream.tell() > history_end, status if tui else None)
                    continue
                latest_config = _load_config(arguments.workload)
                if int(latest_config.get("epoch", 0)) != observed_epoch:
                    # The restored run may reuse sequence numbers and replace log files.
                    # Reopen from the beginning under the new epoch.
                    return command_observe(arguments)
                state, status = _observer_context(config, arguments.slot)
                if tui:
                    _draw_footer(status)
                if no_follow or state in {"complete", "paused", "failed", "stopped"}:
                    print(f"[observer] state={state.upper()}")
                    return 1 if state == "failed" else 0
                time.sleep(0.2)
    finally:
        if tui:
            _draw_footer(None)
            print("\x1b[?4l\x1b[?25h", end="", flush=True)


def _visible_journal_body(value: object) -> str:
    lines = str(value).splitlines()
    while lines and re.fullmatch(
        r"oxide-(?:run:.+|epoch:\d+|stable:[0-9a-f]{32}|routing:[0-9a-f]{32}:[01]{64})",
        lines[-1],
    ):
        lines.pop()
    return "\n".join(lines)


def _queue_snapshot(config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        view = _workflow_client(config, connect_journal(config["socket"]))._view(config["run_id"])
        return {
            "run_id": config["run_id"],
            "state": view.state,
            "entries": [
                {
                    "record_id": record["record_id"],
                    "author": record["author"],
                    "body": _visible_journal_body(record["text"]),
                    "accepted": view.outcomes[int(record["record_id"])][0],
                    "reason": (
                        None
                        if view.outcomes[int(record["record_id"])][0]
                        else str(view.outcomes[int(record["record_id"])][1])
                    ),
                }
                for record in view.records
            ],
        }
    except (JournalError, WorkflowError, json.JSONDecodeError, OSError, IndexError, KeyError):
        return None


def _render_queue(
    snapshot: dict[str, Any] | None, *, color: bool, width: int = 40, header: bool = True
) -> str:
    width = max(20, min(40, width))
    lines: list[tuple[str, str]] = []

    def add(value: object = "", prefix: str = "", code: str = "0") -> None:
        wrapped = textwrap.wrap(
            str(value),
            width=width,
            initial_indent=prefix,
            subsequent_indent="  " if prefix else "",
            break_long_words=True,
            break_on_hyphens=False,
        )
        lines.extend((line, code) for line in wrapped or [""])

    if header:
        add("Oxide JOURNAL", code="1;36")
    if snapshot is None:
        add("WAITING FOR JOURNAL", code="1;33")
        return "\n".join(_style(line, code, color) for line, code in lines) + "\n"
    if header:
        add(snapshot["run_id"])
        state = str(snapshot["state"]).upper()
        add(state, code={"RUNNING": "1;32", "FAILED": "1;31"}.get(state, "1;33"))
        add("-" * width, code="2")
    for entry in snapshot["entries"]:
        add(f"JOURNAL #{entry['record_id']}", code="1;37")
        add(entry["author"], "author: ", "34")
        accepted = bool(entry["accepted"])
        add("accepted" if accepted else "rejected", "status: ", "32" if accepted else "31")
        if not accepted:
            add(entry.get("reason") or "rejected by workflow replay", "reason: ", "31")
        body: list[str] = []
        for raw in _safe(entry["body"]).split("\n"):
            body.extend(textwrap.wrap(raw, width=width, break_on_hyphens=False) or [""])
        body = [*body[:19], body[19][: width - 3] + "..."] if len(body) > 20 else body
        lines.extend((line, "36") for line in body)
        add("-" * width, code="2")
    return "\n".join(_style(line, code, color) for line, code in lines) + "\n"


def command_observe_queue(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    observed_epoch = int(config.get("epoch", 0))
    color = _observer_color(arguments.color)
    width = min(40, shutil.get_terminal_size(fallback=(40, 24)).columns)
    first, waiting, cursor = True, False, 0
    while True:
        latest_config = _load_config(arguments.workload)
        if int(latest_config.get("epoch", 0)) != observed_epoch:
            config = latest_config
            observed_epoch = int(config.get("epoch", 0))
            cursor = 0
            first = True
        snapshot = _queue_snapshot(config)
        if snapshot is not None:
            latest = int(snapshot["entries"][-1]["record_id"]) if snapshot["entries"] else 0
            if latest < cursor:
                cursor = 0
                first = True
            entries = [item for item in snapshot["entries"] if int(item["record_id"]) > cursor]
            if first and not arguments.no_follow:
                entries = entries[-10:]
            if first or entries:
                rendered = _render_queue(
                    {**snapshot, "entries": entries}, color=color, width=width, header=first
                )
                _scroll_lines(rendered, not first)
            if snapshot["entries"]:
                cursor = int(snapshot["entries"][-1]["record_id"])
            first = False
        elif first and not waiting:
            print(_render_queue(None, color=color, width=width), end="", flush=True)
            waiting = True
        if arguments.no_follow:
            return 0
        state = snapshot["state"] if snapshot else "starting"
        if state in {"complete", "paused", "failed", "stopped"}:
            return 1 if state == "failed" else 0
        time.sleep(1)


def command_status(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    client = _workflow_client(config, connect_journal(config["socket"]))
    snapshot = {
        "state": client.search(config["run_id"], "run:state")[0]["state"],
        "tasks": client.search(config["run_id"], "queue:all"),
    }
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


def command_verify_contract(arguments: argparse.Namespace) -> int:
    """Run the harness-owned verifier locally for contract development feedback."""

    target = Path(arguments.target).expanduser().resolve()
    if (
        not target.is_dir()
        or _git(target, "rev-parse", "--is-inside-work-tree", check=False) != "true"
        or Path(_git(target, "rev-parse", "--show-toplevel")).resolve() != target
    ):
        raise HarnessError("target must be a Git worktree root")
    contract = _contract_path(target, arguments.contract)
    try:
        load_contract(contract)
    except ContractError as error:
        raise HarnessError(str(error)) from error
    relative = contract.relative_to(target).as_posix()
    command = invocation(
        target,
        target,
        arguments.operation,
        contract_path=relative,
        root=arguments.root,
        candidate_tree=arguments.candidate_tree,
        prospective_tree=arguments.prospective_tree,
        receipt=arguments.receipt,
        artifact_dir=arguments.artifact_dir,
    )
    return int(subprocess.run(command, cwd=target, check=False).returncode)


def command_verify(_arguments: argparse.Namespace) -> int:
    commands = (
        [sys.executable, "-m", "ruff", "check", "."],
        [sys.executable, "-m", "ruff", "format", "--check", "."],
        [sys.executable, "-m", "pytest", "-q"],
    )
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            return int(result.returncode)
    return 0


def command_validate_concurrency(arguments: argparse.Namespace) -> int:
    seed = arguments.seed if arguments.seed is not None else time.time_ns()
    journal_command = shlex.split(arguments.journal_command or "")
    try:
        min_exact, max_results = validate_search_capacity(
            int(arguments.min_exact), int(arguments.max_results)
        )
    except JournalError as error:
        raise HarnessError(str(error)) from error
    report = run_campaign(
        ROOT,
        ROOT / ".oxide" / "validation",
        workers=arguments.workers,
        rounds=arguments.rounds,
        seed=seed,
        journal_command=journal_command or None,
        min_exact=min_exact,
        max_results=max_results,
    )
    print(
        f"Concurrency validation PASSED: {report['case_count']} cases, "
        f"{report['winner_crash_cases']} winner crashes"
    )
    print(f"Receipt: {report['report_path']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oxide")
    root = parser.add_subparsers(dest="group", required=True)
    verify = root.add_parser("verify")
    verify.set_defaults(handler=command_verify)
    harness = root.add_parser("harness")
    commands = harness.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument(
        "--contract",
        default="verification/contract.toml",
        help="target-relative formal implementation contract (default: verification/contract.toml)",
    )
    run.add_argument("--target", default=str(ROOT.parent / "memory"))
    run.add_argument("--workers", type=int, default=7)
    run.add_argument("--reviews", type=int, help="review quorum; may not weaken contract minimum")
    run.add_argument("--model")
    run.add_argument(
        "--min-exact",
        type=int,
        default=DEFAULT_MIN_EXACT,
        help=f"exact-match capacity floor (default: {DEFAULT_MIN_EXACT})",
    )
    run.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=f"maximum SEARCH results (default: {DEFAULT_MAX_RESULTS})",
    )
    run.add_argument(
        "--journal-command",
        help="external kernel command implementing journal_add/journal_search; default: Python prototype",
    )
    run.add_argument(
        "--concurrency-receipt",
        help="passing campaign receipt to bind to this run; default: .oxide/validation/latest.json",
    )
    run.add_argument("--foreground", action="store_true")
    run.set_defaults(handler=command_run)
    concurrency = commands.add_parser("validate-concurrency")
    concurrency.add_argument("--workers", type=int, default=7)
    concurrency.add_argument("--rounds", type=int, default=6)
    concurrency.add_argument("--seed", type=int)
    concurrency.add_argument("--journal-command")
    concurrency.add_argument(
        "--min-exact",
        type=int,
        default=DEFAULT_MIN_EXACT,
        help=f"exact-match capacity floor (default: {DEFAULT_MIN_EXACT})",
    )
    concurrency.add_argument(
        "--max-results",
        type=int,
        default=DEFAULT_MAX_RESULTS,
        help=f"maximum SEARCH results (default: {DEFAULT_MAX_RESULTS})",
    )
    concurrency.set_defaults(handler=command_validate_concurrency)
    contract_verify = commands.add_parser(
        "verify-contract",
        help="run the harness-owned Verus judge locally (advisory; runs freeze inputs separately)",
    )
    contract_verify.add_argument(
        "operation", choices=("toolchain", "policy", "proof", "gate", "composition")
    )
    contract_verify.add_argument("--target", default=str(Path.cwd()))
    contract_verify.add_argument("--contract", default="verification/contract.toml")
    contract_verify.add_argument("--root")
    contract_verify.add_argument("--candidate-tree")
    contract_verify.add_argument("--prospective-tree")
    contract_verify.add_argument("--receipt", type=Path)
    contract_verify.add_argument("--artifact-dir", type=Path)
    contract_verify.set_defaults(handler=command_verify_contract)
    for name, handler in (("pause", command_pause), ("reset", command_reset)):
        command = commands.add_parser(name)
        command.add_argument("--workload", required=True)
        command.set_defaults(handler=handler)
    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--workload", required=True)
    checkpoint.add_argument("--name", required=True)
    checkpoint.set_defaults(handler=command_checkpoint)
    rewind = commands.add_parser("rewind")
    rewind.add_argument("--workload", required=True)
    rewind.add_argument("--to", required=True)
    rewind.add_argument("--archive", action="store_true")
    rewind.add_argument("--foreground", action="store_true")
    rewind.set_defaults(handler=command_rewind)
    resume = commands.add_parser("resume")
    resume.add_argument("--workload", required=True)
    resume.add_argument("--foreground", action="store_true")
    resume.set_defaults(handler=command_resume)
    launch = commands.add_parser("launch", help=argparse.SUPPRESS)
    launch.add_argument("--workload", required=True)
    launch.set_defaults(handler=command_launch)
    worker = commands.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("--workload", required=True)
    worker.add_argument("--slot", required=True)
    worker.set_defaults(handler=command_worker)
    observe = commands.add_parser("observe")
    observe.add_argument("--workload", required=True)
    observe.add_argument("--slot", required=True)
    observe.add_argument("--no-follow", action="store_true")
    observe.add_argument("--raw", action="store_true")
    observe.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    observe.set_defaults(handler=command_observe)
    queue = commands.add_parser("observe-queue")
    queue.add_argument("--workload", required=True)
    queue.add_argument("--no-follow", action="store_true")
    queue.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    queue.set_defaults(handler=command_observe_queue)
    status = commands.add_parser("status")
    status.add_argument("--workload", required=True)
    status.set_defaults(handler=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        return int(arguments.handler(arguments))
    except (
        ConcurrencyError,
        HarnessError,
        JournalError,
        WorkflowError,
        OSError,
        ValueError,
    ) as error:
        print(f"oxide: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
