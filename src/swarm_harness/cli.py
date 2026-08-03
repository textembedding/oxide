"""Small native macOS launcher for the two-tool swarm."""

from __future__ import annotations

import argparse
import json
import os
import re
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
from pathlib import Path
from typing import Any

from pygments import highlight as pygments_highlight
from pygments.formatters import TerminalFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

from .journal import JournalClient, JournalError, serve_in_thread
from .worker import Worker
from .workflow import WorkflowClient, WorkflowError

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / ".swarm" / "runs"
TIMESTAMP = re.compile(r"^(\[\d{2}:\d{2}:\d{2}\]) (.*)$")
QUEUE_WIDTH = 40


class HarnessError(RuntimeError):
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
            elif ":" in line:
                key, value = line.split(":", 1)
                result[key] = _scalar(value)
                section = None
            else:
                raise HarnessError(f"invalid stage line {number}")
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
            if task is not None and indent == 4 and ":" in line:
                key, value = line.split(":", 1)
                if key in {"depends_on", "checks"}:
                    parsed = [] if not value.strip() else _scalar(value)
                    task_list = key
                    task[key] = parsed if isinstance(parsed, list) else [parsed]
                else:
                    parsed = _scalar(value)
                    task[key] = parsed
                    task_list = None
                continue
            if task is not None and indent == 6 and line.startswith("- ") and task_list:
                task[task_list].append(str(_scalar(line[2:])))
                continue
        if section == "stage_gate" and indent == 2 and line.startswith("- "):
            result["stage_gate"].append(str(_scalar(line[2:])))
            continue
        raise HarnessError(f"unsupported stage line {number}: {line}")
    required = {"stage", "enabled", "goal", "tasks", "stage_gate"}
    if not required <= set(result) or result["enabled"] is not True or not result["tasks"]:
        raise HarnessError("stage is disabled or incomplete")
    for task in result["tasks"]:
        if not {"id", "title", "prompt", "depends_on", "checks"} <= set(task):
            raise HarnessError(f"incomplete task: {task.get('id', '<unknown>')}")
    return result


def _run_dir(workload: str) -> Path:
    return RUNS / workload


def _config_path(workload: str) -> Path:
    return _run_dir(workload) / "run.json"


def _stage_path(workload: str) -> Path:
    return ROOT / "stages" / f"{workload}.yaml"


def _load_config(workload: str) -> dict[str, Any]:
    path = _config_path(workload)
    if not path.is_file():
        raise HarnessError(f"no {workload!r} run exists; start it with harness run")
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


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
    return (
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


def _prepare_repositories(config: dict[str, Any]) -> None:
    target = Path(config["target_repo"])
    if _git(target, "rev-parse", "--is-inside-work-tree") != "true":
        raise HarnessError("target must be a Git worktree")
    if (
        _git(target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        != config["target_branch"]
    ):
        raise HarnessError("target must remain on its staged branch")
    if not _git_succeeds(target, "merge-base", "--is-ancestor", config["base_commit"], "HEAD"):
        raise HarnessError("target branch no longer descends from the staged base")
    if _git(target, "status", "--porcelain=v1", "--untracked-files=no"):
        raise HarnessError("target has tracked changes; commit or stash them first")
    worker_root = Path(config["run_dir"]) / "workers"
    worker_root.mkdir(parents=True, exist_ok=True)
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
            _git(clone, "config", "user.name", "Swarm Worker")
            _git(clone, "config", "user.email", "swarm-worker@localhost")
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
            return False, f"check failed ({result.returncode}): {check}: {output[-1200:]}"
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
    if _git(target, "status", "--porcelain=v1", "--untracked-files=no"):
        raise HarnessError("cannot merge over tracked target changes")
    if _git(target, "rev-parse", "--verify", f"refs/heads/{branch}", check=False) != head:
        _merge_failed(config, client, task, "PR branch no longer matches approved head")
        return
    checks = [str(item) for item in task.get("checks", [])]
    before = _git(target, "rev-parse", "HEAD")
    if _git_succeeds(target, "merge-base", "--is-ancestor", head, before):
        passed, reason = _run_checks(target, checks, log)
        if not passed:
            _merge_failed(config, client, task, reason)
            return
        merged = before
        tree = _git(target, "rev-parse", "HEAD^{tree}")
    else:
        with tempfile.TemporaryDirectory(
            prefix="verify-merge-", dir=Path(config["run_dir"])
        ) as temporary:
            verification = Path(temporary) / "repo"
            clone = subprocess.run(
                ["git", "clone", "--no-hardlinks", str(target), str(verification)],
                text=True,
                capture_output=True,
                check=False,
            )
            if clone.returncode:
                raise HarnessError(clone.stderr.strip() or "could not clone merge verifier")
            _git(verification, "checkout", target_branch)
            candidate = f"refs/remotes/origin/{branch}"
            if _git(verification, "rev-parse", "--verify", candidate, check=False) != head:
                _merge_failed(config, client, task, "verification clone saw a different PR head")
                return
            prospective = subprocess.run(
                [
                    "git",
                    "-C",
                    str(verification),
                    "-c",
                    "user.name=Swarm Merge",
                    "-c",
                    "user.email=swarm-merge@localhost",
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
                    prospective.stderr.strip() or "PR conflicts with current main",
                )
                return
            passed, reason = _run_checks(verification, checks, log)
            if not passed:
                _merge_failed(config, client, task, reason)
                return
            expected_tree = _git(verification, "rev-parse", "HEAD^{tree}")
        if _git(target, "rev-parse", "HEAD") != before:
            raise HarnessError("target changed during prospective merge verification")
        if _git(target, "rev-parse", f"refs/heads/{branch}") != head:
            raise HarnessError("PR head changed during prospective merge verification")
        actual = subprocess.run(
            [
                "git",
                "-C",
                str(target),
                "-c",
                "user.name=Swarm Merge",
                "-c",
                "user.email=swarm-merge@localhost",
                "merge",
                "--no-ff",
                "--no-edit",
                branch,
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if actual.returncode:
            _git(target, "merge", "--abort", check=False)
            _merge_failed(config, client, task, actual.stderr.strip() or "merge failed")
            return
        merged = _git(target, "rev-parse", "HEAD")
        tree = _git(target, "rev-parse", "HEAD^{tree}")
        if tree != expected_tree:
            raise HarnessError("actual merge tree differs from the verified prospective tree")
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
    if _git(target, "status", "--porcelain=v1", "--untracked-files=no"):
        raise HarnessError("cannot publish over tracked target changes")
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
    stage = load_stage(config["stage_path"])
    passed, reason = _run_checks(target, [str(item) for item in stage["stage_gate"]], log)
    if not passed:
        raise HarnessError(f"stage gate failed: {reason}")
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
    if os.environ.get("SWARM_NO_TERMINAL") == "1":
        subprocess.Popen(
            arguments,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return
    if sys.platform != "darwin":
        raise HarnessError("visible worker terminals require macOS Terminal")
    script = 'tell application "Terminal" to do script ' + json.dumps(_terminal_command(arguments))
    result = subprocess.run(
        ["osascript", "-e", script], text=True, capture_output=True, check=False
    )
    if result.returncode:
        raise HarnessError(result.stderr.strip() or "could not open Terminal")


def _wait_socket(path: Path, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise HarnessError(f"journal socket did not appear: {path}")
        time.sleep(0.1)


def _using_journal(config: dict[str, Any], call: Callable[[WorkflowClient], Any]) -> Any:
    socket_path = Path(config["socket"])
    if socket_path.exists():
        try:
            return call(WorkflowClient(JournalClient(socket_path, timeout=0.5)))
        except (ConnectionError, OSError):
            pass
    server, thread = serve_in_thread(config["database"], socket_path)
    try:
        return call(WorkflowClient(JournalClient(socket_path)))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _run_state(config: dict[str, Any]) -> str:
    try:
        return str(
            _using_journal(
                config,
                lambda client: client.search(config["run_id"], "run:state")[0]["state"],
            )
        )
    except (JournalError, OSError, IndexError):
        return "starting"


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
    rows: list[tuple[int, str]] = []
    for pid, command in _process_table():
        if swarmctl in command and f"harness worker --workload {workload}" in command:
            rows.append((pid, "worker"))
        elif swarmctl in command and f"harness launch --workload {workload}" in command:
            rows.append((pid, "launcher"))
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
    return [
        str(ROOT / "swarmctl"),
        "harness",
        "worker",
        "--workload",
        workload,
        "--slot",
        slot,
    ]


class _Supervisor:
    def __init__(self, config: dict[str, Any], log: Callable[[str], None]) -> None:
        self.config = config
        self.log = log
        self.expected = {f"worker-{index}" for index in range(int(config["workers"]))}
        self.starting: dict[str, float] = {}

    def _launch(self, slot: str) -> None:
        _launch_terminal(_worker_argv(str(self.config["workload"]), slot))
        self.starting[slot] = time.monotonic() + 5
        self.log(f"launched {slot}")

    def tick(self) -> None:
        live = _live_slots(self.config) & self.expected
        for slot in live:
            self.starting.pop(slot, None)
        now = time.monotonic()
        for slot in sorted(self.expected - live):
            if slot not in self.starting or now >= self.starting[slot]:
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
    print(f"Started {config['workload']} in a native Terminal window.")
    print(f"Observe: ./swarmctl harness observe --workload {config['workload']} --slot worker-0")
    print(f"Queue:   ./swarmctl harness observe-queue --workload {config['workload']}")
    return 0


def command_run(arguments: argparse.Namespace) -> int:
    if arguments.resume:
        return command_resume(arguments)
    stage = load_stage(_stage_path(arguments.workload))
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
    if (
        not target.is_dir()
        or _git(target, "rev-parse", "--is-inside-work-tree", check=False) != "true"
    ):
        raise HarnessError("target must be a Git worktree")
    target_branch = _git(target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if not target_branch:
        raise HarnessError("target must have a checked-out branch")
    if _git(target, "status", "--porcelain=v1", "--untracked-files=no"):
        raise HarnessError("target has tracked changes; commit or stash them first")
    base_commit = _git(target, "rev-parse", "HEAD")
    run_id = f"{arguments.workload}-{time.strftime('%Y%m%d-%H%M%S')}"
    config = {
        "schema_version": 4,
        "run_id": run_id,
        "workload": arguments.workload,
        "stage_path": str(_stage_path(arguments.workload)),
        "target_repo": str(target),
        "target_branch": target_branch,
        "base_commit": base_commit,
        "run_dir": str(run_dir),
        "database": str(run_dir / "journal.sqlite3"),
        "socket": str(run_dir / "journal.sock"),
        "workers": arguments.workers,
        "required_reviews": arguments.reviews,
        "model": arguments.model,
        "branch_prefix": f"codex/swarm-{_slug(run_id)}",
        "stage": stage["stage"],
    }
    _atomic_json(_config_path(arguments.workload), config)
    return _start(config, arguments.foreground)


def command_launch(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    log = _Log(Path(config["run_dir"]) / "logs" / "orchestrator.log")
    server, thread = serve_in_thread(config["database"], config["socket"])
    client = WorkflowClient(JournalClient(config["socket"]))
    try:
        stage = load_stage(config["stage_path"])
        stage["required_reviews"] = int(config["required_reviews"])
        for task in stage["tasks"]:
            task["branch"] = f"{config['branch_prefix']}/{_slug(str(task['id']))}"
        result = client.add(
            config["run_id"],
            "launcher",
            f"bootstrap: run:{config['run_id']}\nstage-json: {json.dumps(stage, separators=(',', ':'))}",
        )
        log(f"run {config['run_id']}: {'created' if result['saved'] else 'resumed'}")
        state = str(result["state"])
        supervisor: _Supervisor | None = None
        if state == "running":
            _prepare_repositories(config)
            supervisor = _Supervisor(config, log)
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
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def command_worker(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    _wait_socket(Path(config["socket"]))
    log = _Log(Path(config["run_dir"]) / "logs" / f"{arguments.slot}.log")
    worker = Worker(
        WorkflowClient(JournalClient(config["socket"])),
        config["run_id"],
        arguments.slot,
        Path(config["run_dir"]) / "workers" / arguments.slot,
        config["target_branch"],
        config["target_repo"],
        model=config.get("model"),
        log=log,
    )
    state = worker.run()
    log(f"slot stopped: {state}")
    return 0 if state in {"paused", "publishing", "complete", "stopped"} else 1


def command_pause(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    state = _run_state(config)
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


def _style(value: object, code: str, color: bool) -> str:
    text = str(value)
    return f"\033[{code}m{text}\033[0m" if color else text


def _safe(value: object) -> str:
    rendered: list[str] = []
    for character in str(value):
        codepoint = ord(character)
        if character == "\n":
            rendered.append(character)
        elif character == "\t":
            rendered.append("\\t")
        elif character == "\r":
            rendered.append("\\r")
        elif codepoint < 32 or 127 <= codepoint <= 159:
            rendered.append(f"\\x{codepoint:02x}")
        elif codepoint in {0x2028, 0x2029} or 0xD800 <= codepoint <= 0xDFFF:
            rendered.append(f"\\u{codepoint:04x}")
        else:
            rendered.append(character)
    return "".join(rendered)


def _code(value: object, language: str, color: bool) -> str:
    safe = _safe(value).rstrip("\n")
    if not color or not safe:
        return safe
    try:
        lexer = get_lexer_by_name(language) if language else guess_lexer(safe)
        return pygments_highlight(safe, lexer, TerminalFormatter()).removesuffix("\n")
    except ClassNotFound:
        return safe


def _indent(value: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in value.splitlines())


def _tool_value(item: dict[str, Any], phase: str, color: bool) -> str:
    server = item.get("server", "")
    tool = item.get("tool", item.get("name", ""))
    heading = f"{_style('TOOL', '1;36', color)} {phase} {_style(f'{server}.{tool}'.strip('.'), '1;34', color)}"
    arguments = item.get("arguments", item.get("input", {}))
    result = item.get("result", item.get("output"))
    if server == "journal" and isinstance(arguments, dict) and "yaml" in arguments:
        blocks = [heading, "input.yaml:\n" + _indent(_code(arguments["yaml"], "yaml", color))]
        if isinstance(result, dict):
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                blocks.append(
                    "output.yaml:\n" + _indent(_code(content[0].get("text", ""), "yaml", color))
                )
        return "\n".join(blocks)
    return heading + "\n" + _indent(json.dumps(arguments, indent=2, sort_keys=True))


def _event_value(event: dict[str, Any], color: bool) -> str:
    event_type = str(event.get("type", "event"))
    phase = event_type.split(".", 1)[-1].upper()
    item = event.get("item")
    if isinstance(item, dict) and item.get("type") in {"mcp_tool_call", "tool_call"}:
        return _tool_value(item, phase, color)
    if isinstance(item, dict) and item.get("type") in {"command_execution", "command"}:
        command = item.get("command", item.get("cmd", ""))
        output = item.get("aggregated_output", item.get("output", ""))
        parts = [
            f"{_style('COMMAND', '1;36', color)} {phase}",
            "command:\n" + _indent(_code(command, "bash", color)),
        ]
        if output:
            language = "diff" if "git diff" in str(command) else ""
            parts.append("output:\n" + _indent(_code(output, language, color)))
        return "\n".join(parts)
    if isinstance(item, dict) and item.get("type") in {"reasoning", "agent_message"}:
        label = "REASONING" if item["type"] == "reasoning" else "AGENT"
        return f"{label} [{phase}]\n" + _indent(_code(item.get("text", ""), "markdown", color))
    return _code(json.dumps(event, ensure_ascii=False, indent=2), "json", color)


def highlight_stream_line(line: str, *, color: bool, raw: bool = False) -> str:
    if raw:
        return line
    match = TIMESTAMP.match(line)
    timestamp, body = (match.group(1), match.group(2)) if match else ("", line)
    try:
        rendered = _event_value(json.loads(body), color)
    except json.JSONDecodeError:
        rendered = _safe(body)
    if not timestamp:
        return rendered
    prefix = _style(timestamp, "2", color) + " "
    return prefix + rendered.replace("\n", "\n" + " " * (len(timestamp) + 1))


def _observer_color(mode: str) -> bool:
    return mode == "always" or mode == "auto" and sys.stdout.isatty()


def command_observe(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    allowed = {"orchestrator"} | {f"worker-{i}" for i in range(int(config["workers"]))}
    if arguments.slot not in allowed:
        raise HarnessError(f"unknown slot: {arguments.slot}")
    path = Path(config["run_dir"]) / "logs" / f"{arguments.slot}.log"
    color = _observer_color(getattr(arguments, "color", "auto"))
    raw = bool(getattr(arguments, "raw", False))
    no_follow = bool(arguments.no_follow)
    index = 0 if arguments.slot == "orchestrator" else int(arguments.slot.split("-")[1]) + 1
    print(f"[observer] slot={arguments.slot} index={index}")
    print(f"[observer] invocation={config['run_id']}:{arguments.slot} slot={arguments.slot}")
    while not path.exists():
        if no_follow:
            return 0
        time.sleep(0.1)
    with path.open("r", encoding="utf-8", errors="replace") as stream:
        while True:
            line = stream.readline()
            if line:
                print(highlight_stream_line(line.rstrip("\n"), color=color, raw=raw), flush=True)
                continue
            state = _run_state(config)
            if no_follow or state in {"complete", "paused", "failed", "stopped"}:
                print(f"[observer] state={state.upper()}")
                return 1 if state == "failed" else 0
            time.sleep(0.2)


def _queue_snapshot(config: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return _using_journal(
            config,
            lambda client: {
                "run_id": config["run_id"],
                "state": client.search(config["run_id"], "run:state")[0]["state"],
                "tasks": client.search(config["run_id"], "queue:all"),
            },
        )
    except (JournalError, OSError, IndexError):
        return None


def _render_queue(snapshot: dict[str, Any] | None, *, color: bool, width: int = 40) -> str:
    width = max(20, min(QUEUE_WIDTH, width))
    lines: list[str] = []

    def add(value: object = "", prefix: str = "") -> None:
        lines.extend(
            textwrap.wrap(
                str(value),
                width=width,
                initial_indent=prefix,
                subsequent_indent="  " if prefix else "",
                break_long_words=True,
                break_on_hyphens=False,
            )
            or [""]
        )

    add("SWARM QUEUE")
    if snapshot is None:
        add("WAITING FOR JOURNAL")
        return "\n".join(lines) + "\n"
    add(snapshot["run_id"])
    add(str(snapshot["state"]).upper())
    add("-" * width)
    priority = {"working": 0, "ready": 1, "complete": 2}
    tasks = [task for task in snapshot["tasks"] if task["state"] != "blocked"]
    tasks.sort(key=lambda task: priority.get(str(task["state"]), 3))
    counts: dict[str, int] = {}
    for task in tasks:
        state = str(task["state"]).upper()
        counts[state] = counts.get(state, 0) + 1
        add(state)
        add(task["task_id"])
        if task.get("role") and task["role"] != "task":
            add(task["role"], "role: ")
        if task.get("worker_id"):
            add(task["worker_id"], "owner: ")
        if task.get("required_reviews"):
            add(
                f"{task.get('approvals', 0)}/{task['required_reviews']}",
                "reviews: ",
            )
        if task.get("commit_sha"):
            add(str(task["commit_sha"])[:12], "commit: ")
        add("-" * width)
    add("SUMMARY")
    for state in ("WORKING", "READY", "COMPLETE"):
        if counts.get(state):
            add(counts[state], f"{state.lower()}: ")
    return (
        "\n".join(_style(line, "1;36" if line == "SWARM QUEUE" else "0", color) for line in lines)
        + "\n"
    )


def command_observe_queue(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    color = _observer_color(arguments.color)
    width = min(QUEUE_WIDTH, shutil.get_terminal_size(fallback=(QUEUE_WIDTH, 24)).columns)
    last = None
    while True:
        snapshot = _queue_snapshot(config)
        rendered = _render_queue(snapshot, color=color, width=width)
        if rendered != last:
            print(rendered, end="", flush=True)
            last = rendered
        if arguments.no_follow:
            return 0
        state = snapshot["state"] if snapshot else "starting"
        if state in {"complete", "paused", "failed", "stopped"}:
            return 1 if state == "failed" else 0
        time.sleep(1)


def command_status(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    snapshot = _queue_snapshot(config)
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
    run.add_argument("--foreground", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.set_defaults(handler=command_run)
    for name, handler in (("pause", command_pause), ("reset", command_reset)):
        command = commands.add_parser(name)
        command.add_argument("--workload", required=True)
        command.set_defaults(handler=handler)
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
    except (HarnessError, JournalError, WorkflowError, OSError, ValueError) as error:
        print(f"swarmctl: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
