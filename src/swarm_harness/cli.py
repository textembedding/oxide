import argparse
import fcntl
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

import yaml
from pygments import highlight as pygments_highlight
from pygments.formatters import TerminalFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from .concurrency import ConcurrencyError, implementation_digest, run_campaign, validate_receipt
from .journal_backend import (
    DEFAULT_MAX_RESULTS,
    DEFAULT_MIN_EXACT,
    JournalError,
    JournalPort,
    connect_journal,
    start_journal,
    validate_search_capacity,
)
from .worker import Worker, worktree_diff
from .workflow import WorkflowClient, WorkflowError

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / ".swarm" / "runs"
CHECKPOINTS = ROOT / ".swarm" / "checkpoints"
TARGET_HARNESS_DIRECTORY = "swarm-harness"
_WORKLOAD_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class HarnessError(RuntimeError):
    pass


def _validate_stage(loaded: object, source: object) -> dict[str, Any]:
    if not isinstance(loaded, dict):
        raise HarnessError("workload contract must be a YAML mapping")
    result = dict(loaded)
    required = {"stage", "enabled", "goal", "tasks", "stage_gate"}
    if not required <= set(result) or result["enabled"] is not True:
        raise HarnessError("workload is disabled or incomplete")
    if not isinstance(result["goal"], str) or not result["goal"].strip():
        raise HarnessError("workload goal must be a nonempty string")
    if not isinstance(result["tasks"], list) or not result["tasks"]:
        raise HarnessError("workload must contain tasks")
    if (
        not isinstance(result["stage_gate"], list)
        or not result["stage_gate"]
        or not all(isinstance(command, str) and command.strip() for command in result["stage_gate"])
    ):
        raise HarnessError("workload stage_gate must contain commands")
    identifiers: set[str] = set()
    for task in result["tasks"]:
        if not isinstance(task, dict):
            raise HarnessError("each workload task must be a mapping")
        if not {"id", "title", "prompt", "depends_on", "checks"} <= set(task):
            raise HarnessError(f"incomplete task: {task.get('id', '<unknown>')}")
        identifier = str(task["id"])
        if not _WORKLOAD_NAME.fullmatch(identifier) or identifier in identifiers:
            raise HarnessError(f"task identifiers must be unique safe names: {identifier!r}")
        identifiers.add(identifier)
        if (
            not isinstance(task["title"], str)
            or not task["title"].strip()
            or not isinstance(task["prompt"], str)
            or not task["prompt"].strip()
        ):
            raise HarnessError(f"task {identifier} title and prompt must be nonempty strings")
        if not isinstance(task["depends_on"], list) or not all(
            isinstance(item, str) for item in task["depends_on"]
        ):
            raise HarnessError(f"task {identifier} dependencies must be a list of task IDs")
        if (
            not isinstance(task["checks"], list)
            or not task["checks"]
            or not all(isinstance(command, str) and command.strip() for command in task["checks"])
        ):
            raise HarnessError(f"task {identifier} checks must contain commands")
    for task in result["tasks"]:
        unknown = set(task["depends_on"]) - identifiers
        if unknown or task["id"] in task["depends_on"]:
            raise HarnessError(f"task {task['id']} has invalid dependencies: {sorted(unknown)}")
    dependencies = {str(task["id"]): set(task["depends_on"]) for task in result["tasks"]}
    remaining = set(dependencies)
    while remaining:
        ready = {identifier for identifier in remaining if not dependencies[identifier] & remaining}
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise HarnessError(f"workload dependency graph contains a cycle: {cycle}")
        remaining -= ready
    try:
        json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise HarnessError(
            "workload contract must contain only finite JSON-compatible values"
        ) from error
    return result


def load_stage(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        loaded = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise HarnessError(f"invalid workload contract {source}: {error}") from error
    return _validate_stage(loaded, source)


def _run_dir(workload: str) -> Path:
    if _WORKLOAD_NAME.fullmatch(workload) is None:
        raise HarnessError("workload must be a safe name, not a path")
    return RUNS / workload


def _config_path(workload: str) -> Path:
    return _run_dir(workload) / "run.json"


def _stage_path(target: str | Path, workload: str) -> Path:
    if _WORKLOAD_NAME.fullmatch(workload) is None:
        raise HarnessError("workload must be a safe name, not a path")
    target_root = Path(target).resolve()
    root = (target_root / TARGET_HARNESS_DIRECTORY).resolve()
    if not root.is_relative_to(target_root):
        raise HarnessError("target harness directory must not escape through a symlink")
    path = (root / f"{workload}.yaml").resolve()
    if not path.is_relative_to(root):
        raise HarnessError("workload contract escaped the target harness directory")
    return path


def _repository_identity(target: Path) -> str:
    remote = _git(target, "remote", "get-url", "origin", check=False)
    return remote or str(target.resolve())


def _frozen_workload_ref(target: Path, base_commit: str, workload: str) -> dict[str, Any]:
    path = _stage_path(target, workload)
    relative = path.relative_to(target).as_posix()
    blob = _git(target, "rev-parse", f"{base_commit}:{relative}")
    tree = _git(target, "rev-parse", f"{base_commit}:{TARGET_HARNESS_DIRECTORY}")
    return {
        "schema": "SwarmWorkloadRefV1",
        "target_repository": _repository_identity(target),
        "base_commit": base_commit,
        "workload_path": relative,
        "workload_blob": blob,
        "harness_tree": tree,
        "harness_version": implementation_digest(ROOT),
    }


def _load_frozen_stage(config: dict[str, Any]) -> dict[str, Any]:
    target = Path(str(config["target_repo"])).resolve()
    reference = config.get("workload_ref")
    if not isinstance(reference, dict):
        raise HarnessError("run has no frozen workload reference")
    base = str(reference.get("base_commit", ""))
    path = str(reference.get("workload_path", ""))
    if reference.get("target_repository") != _repository_identity(target):
        raise HarnessError("target repository identity changed since run creation")
    if _git(target, "rev-parse", f"{base}:{path}") != reference.get("workload_blob"):
        raise HarnessError("frozen workload blob no longer matches the staged base")
    if _git(target, "rev-parse", f"{base}:{TARGET_HARNESS_DIRECTORY}") != reference.get(
        "harness_tree"
    ):
        raise HarnessError("frozen target harness tree no longer matches the staged base")
    if _git(target, "rev-parse", f"HEAD:{TARGET_HARNESS_DIRECTORY}") != reference.get(
        "harness_tree"
    ):
        raise HarnessError("workload/specification area changed; start a new run")
    if _git(
        target,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        TARGET_HARNESS_DIRECTORY,
    ):
        raise HarnessError("workload/specification area changed; start a new run")
    try:
        loaded = yaml.safe_load(_git(target, "show", f"{base}:{path}"))
    except yaml.YAMLError as error:
        raise HarnessError(f"frozen workload is invalid: {error}") from error
    return _validate_stage(loaded, f"{base}:{path}")


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
    expected_prefix = f"codex/swarm-{_slug(str(run_id))}"
    if (
        config.get("schema_version") != 7
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
    target = Path(str(config.get("target_repo", ""))).resolve()
    config["stage_path"] = str(_stage_path(target, workload))
    return config


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


@contextmanager
def _exclusive_run(workload: str):
    path = ROOT / ".swarm" / "locks" / f"{workload}.lock"
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
        remote = "refs/remotes/origin/swarm-base"
        _git(
            clone,
            "fetch",
            "origin",
            f"refs/heads/{config['target_branch']}:{remote}",
        )
        if not _git(clone, "status", "--porcelain=v1", "--untracked-files=all"):
            _git(clone, "checkout", "-B", "swarm-worker", remote)


def _run_checks(
    repository: Path, checks: list[str], log: Callable[[str], None]
) -> tuple[bool, str]:
    for check in checks:
        log(f"verify: {check}")
        result = subprocess.run(
            ["/bin/zsh", "-lc", check],
            cwd=repository,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            output = (result.stdout + result.stderr).strip()
            return False, f"check failed ({result.returncode}): {check}: {output[-1800:]}"
    return True, ""


def _merge_failed(
    config: dict[str, Any], client: WorkflowClient, task: dict[str, Any], reason: str
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
        if not _git_succeeds(
            verification,
            "diff",
            "--quiet",
            before,
            "HEAD",
            "--",
            TARGET_HARNESS_DIRECTORY,
        ):
            _merge_failed(
                config,
                client,
                task,
                f"candidate modifies immutable {TARGET_HARNESS_DIRECTORY}/ workload inputs",
            )
            return
        passed, reason = _run_checks(
            verification, [str(value) for value in task.get("checks", [])], log
        )
        if not passed:
            _merge_failed(config, client, task, reason)
            return
        verified_commit = _git(verification, "rev-parse", "HEAD")
        expected_tree = _git(verification, "rev-parse", "HEAD^{tree}")
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
    stage = client.workload(config["run_id"])
    with tempfile.TemporaryDirectory(prefix="final-check-", dir=config["run_dir"]) as temporary:
        verification = Path(temporary) / "repository"
        cloned = subprocess.run(
            ["git", "clone", "--no-hardlinks", str(target), str(verification)],
            text=True,
            capture_output=True,
            check=False,
        )
        if cloned.returncode:
            raise HarnessError(cloned.stderr.strip() or "could not create final-check clone")
        _git(verification, "checkout", target_branch)
        if _git(verification, "rev-parse", "HEAD") != tip:
            raise HarnessError("final-check clone does not match the target frontier")
        passed, reason = _run_checks(
            verification, [str(value) for value in stage["stage_gate"]], log
        )
        if not passed:
            raise HarnessError(f"workload final gate failed: {reason}")
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
    swarmctl = str(ROOT / "swarmctl")
    worker_root = str((Path(config["run_dir"]) / "workers").resolve())
    workload_argument = re.compile(
        r"(?:^|\s)--workload(?:=|\s+)" + re.escape(workload) + r"(?:\s|$)"
    )
    rows: list[tuple[int, str]] = []
    for pid, command in _process_table():
        belongs_to_run = swarmctl in command and workload_argument.search(command) is not None
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
        re.escape(str(ROOT / "swarmctl"))
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
    return [str(ROOT / "swarmctl"), "harness", "worker", "--workload", workload, "--slot", slot]


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
    _launch_terminal(
        [str(ROOT / "swarmctl"), "harness", "launch", "--workload", config["workload"]]
    )
    print(f"Started {config['workload']} in the background.")
    print(f"Observe: ./swarmctl harness observe --workload {config['workload']} --slot worker-0")
    print(f"Queue:   ./swarmctl harness observe-queue --workload {config['workload']}")
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
    if arguments.resume:
        return command_resume(arguments)
    target = Path(arguments.target).expanduser().resolve()
    run_dir = _run_dir(arguments.workload)
    if _config_path(arguments.workload).exists():
        raise HarnessError("run already exists; use resume or reset")
    if arguments.workers < 1:
        raise HarnessError("workers must be positive")
    if not 1 <= arguments.reviews <= 16:
        raise HarnessError("reviews must be between 1 and 16")
    if arguments.workers < arguments.reviews + 1:
        raise HarnessError("workers must include an author plus distinct reviewers")
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
    stage_path = _stage_path(target, arguments.workload)
    stage = load_stage(stage_path)
    stage_relative = str(stage_path.relative_to(target))
    if not _git_succeeds(target, "ls-files", "--error-unmatch", "--", stage_relative):
        raise HarnessError(
            f"workload contract must be committed inside {TARGET_HARNESS_DIRECTORY}/: "
            f"{stage_relative}"
        )
    journal_command = shlex.split(getattr(arguments, "journal_command", "") or "")
    receipt_path = Path(
        getattr(arguments, "concurrency_receipt", None)
        or ROOT / ".swarm" / "validation" / "latest.json"
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
    workload_ref = _frozen_workload_ref(target, base_commit, arguments.workload)
    run_id = f"{arguments.workload}-{time.strftime('%Y%m%d-%H%M%S')}"
    config = {
        "schema_version": 7,
        "run_id": run_id,
        "workload": arguments.workload,
        "stage_path": str(stage_path),
        "target_repo": str(target),
        "target_branch": target_branch,
        "base_commit": base_commit,
        "git_identity": git_identity,
        "run_dir": str(run_dir),
        "database": str(run_dir / "journal.sqlite3"),
        "socket": str(run_dir / "journal.sock"),
        "workers": arguments.workers,
        "required_reviews": arguments.reviews,
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
        "branch_prefix": f"codex/swarm-{_slug(run_id)}",
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
    _atomic_json(_config_path(arguments.workload), config)
    _snapshot_checkpoint(config, "initial")
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
        if state == "running" and not client.search(config["run_id"], "control: drain-reviews"):
            client.add(config["run_id"], "launcher", "control: drain-reviews")
        capacity_record = (
            f"control: worker-capacity\nworkers: {int(config['workers'])}\nterminal-blockers: true"
        )
        if state == "running" and not any(
            item.get("body") == capacity_record
            for item in client.search(config["run_id"], "control: worker-capacity")
        ):
            client.add(config["run_id"], "launcher", capacity_record)
        if state == "running" and not client.search(
            config["run_id"], "control: worker-verification"
        ):
            client.add(config["run_id"], "launcher", "control: worker-verification")
        if state == "running" and not client.search(config["run_id"], "control: reusable-slots"):
            client.add(config["run_id"], "launcher", "control: reusable-slots")
        supervisor: _Supervisor | None = None
        if state == "running":
            _prepare_repositories(config)
            supervisor = _Supervisor(config, client, log)
        elif state != "publishing":
            log(f"run {config['run_id']}: {state.upper()}")
            return 0 if state in {"paused", "complete", "stopped"} else 1
        while True:
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
    finally:
        runtime.close()


def command_worker(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
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
        manifest.get("schema") != "SwarmDestructiveCheckpointV1"
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
        "schema": "SwarmDestructiveCheckpointV1",
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
                / ".swarm"
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
        r"swarm-(?:run:.+|epoch:\d+|stable:[0-9a-f]{32}|routing:[0-9a-f]{32}:[01]{64})",
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
        add("SWARM JOURNAL", code="1;36")
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
        ROOT / ".swarm" / "validation",
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
    parser = argparse.ArgumentParser(prog="swarmctl")
    root = parser.add_subparsers(dest="group", required=True)
    verify = root.add_parser("verify")
    verify.set_defaults(handler=command_verify)
    harness = root.add_parser("harness")
    commands = harness.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--workload", required=True)
    run.add_argument("--target", default=str(ROOT.parent / "memory"))
    run.add_argument("--workers", type=int, default=7)
    run.add_argument("--reviews", type=int, default=3)
    run.add_argument("--model")
    run.add_argument("--min-exact", type=int, default=DEFAULT_MIN_EXACT)
    run.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    run.add_argument(
        "--journal-command",
        help="external kernel command implementing journal_add/journal_search; default: Python prototype",
    )
    run.add_argument(
        "--concurrency-receipt",
        help="passing campaign receipt to bind to this run; default: .swarm/validation/latest.json",
    )
    run.add_argument("--foreground", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.set_defaults(handler=command_run)
    concurrency = commands.add_parser("validate-concurrency")
    concurrency.add_argument("--workers", type=int, default=7)
    concurrency.add_argument("--rounds", type=int, default=6)
    concurrency.add_argument("--seed", type=int)
    concurrency.add_argument("--journal-command")
    concurrency.add_argument("--min-exact", type=int, default=DEFAULT_MIN_EXACT)
    concurrency.add_argument("--max-results", type=int, default=DEFAULT_MAX_RESULTS)
    concurrency.set_defaults(handler=command_validate_concurrency)
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
        print(f"swarmctl: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
