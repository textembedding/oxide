"""Native macOS command line for running and observing the clean-room harness."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

from .controller import Controller, ControllerError, load_stage
from .journal_client import JournalClient
from .protocol import JournalError, ProtocolError
from .sqlite_service import serve_in_thread
from .worker import Worker

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / ".swarm" / "runs"


def _run_dir(workload: str) -> Path:
    return RUNS / workload


def _config_path(workload: str) -> Path:
    return _run_dir(workload) / "run.json"


def _load_config(workload: str) -> dict:
    path = _config_path(workload)
    if not path.is_file():
        raise ControllerError(f"no {workload!r} run exists; start it with harness run")
    return json.loads(path.read_text(encoding="utf-8"))


def _stage_path(workload: str) -> Path:
    name = "stage0" if workload == "pilot" else workload
    return ROOT / "stages" / f"{name}.yaml"


def _atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _terminal_command(arguments: list[str]) -> str:
    return f"cd {shlex.quote(str(ROOT))} && exec {shlex.join(arguments)}"


def _launch_terminal(arguments: list[str]) -> None:
    command = _terminal_command(arguments)
    if os.environ.get("SWARM_NO_TERMINAL") == "1":
        subprocess.Popen(arguments, cwd=ROOT, start_new_session=True)
        return
    if sys.platform != "darwin":
        raise ControllerError("visible worker terminals currently require macOS Terminal")
    script = 'tell application "Terminal" to do script ' + json.dumps(command)
    completed = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise ControllerError(completed.stderr.strip() or "could not open Terminal")


class _Log:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = path.open("a", encoding="utf-8", buffering=1)
        self.lock = threading.Lock()

    def __call__(self, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        with self.lock:
            self.stream.write(line + "\n")
            print(line, flush=True)


def _wait_socket(path: Path, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise ControllerError(f"journal socket did not appear: {path}")
        time.sleep(0.1)


def command_run(arguments: argparse.Namespace) -> int:
    stage = load_stage(_stage_path(arguments.workload))
    target = Path(arguments.target).expanduser().resolve()
    run_dir = _run_dir(arguments.workload)
    config_path = _config_path(arguments.workload)
    if config_path.exists() and not arguments.resume:
        raise ControllerError(
            f"run already exists at {run_dir}; use --resume or choose another workload"
        )
    run_id = (
        _load_config(arguments.workload)["run_id"]
        if arguments.resume and config_path.exists()
        else f"{arguments.workload}-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    config = {
        "schema_version": 1,
        "run_id": run_id,
        "workload": arguments.workload,
        "stage_path": str(_stage_path(arguments.workload)),
        "target_repo": str(target),
        "run_dir": str(run_dir),
        "database": str(run_dir / "journal.sqlite3"),
        "socket": str(run_dir / "journal.sock"),
        "workers": arguments.workers,
        "model": arguments.model,
        "stage": stage["stage"],
    }
    _atomic_json(config_path, config)
    argv = [str(ROOT / "swarmctl"), "harness", "orchestrate", "--workload", arguments.workload]
    if arguments.foreground:
        return command_orchestrate(argparse.Namespace(workload=arguments.workload))
    _launch_terminal(argv)
    print(f"Started {arguments.workload} in a native Terminal window.")
    print(f"Observe: ./swarmctl harness observe --workload {arguments.workload} --slot orchestrator")
    print(f"Worker:  ./swarmctl harness observe --workload {arguments.workload} --slot worker-0")
    return 0


def command_orchestrate(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    run_dir = Path(config["run_dir"])
    log = _Log(run_dir / "logs" / "orchestrator.log")
    server, thread = serve_in_thread(config["database"], config["socket"])
    client = JournalClient(config["socket"])
    stage = load_stage(config["stage_path"])
    controller = Controller(
        client,
        config["run_id"],
        config["workload"],
        stage,
        config["target_repo"],
        run_dir,
        log,
    )
    try:
        controller.seed()
        controller.prepare_runnable()
        for index in range(int(config["workers"])):
            slot = f"worker-{index}"
            argv = [
                str(ROOT / "swarmctl"),
                "harness",
                "worker",
                "--workload",
                arguments.workload,
                "--slot",
                slot,
            ]
            _launch_terminal(argv)
            log(f"launched {slot}")
        while True:
            state = controller.tick()
            if state in {"complete", "failed", "stopped"}:
                return 0 if state == "complete" else 1
            time.sleep(0.5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def command_worker(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    socket_path = Path(config["socket"])
    _wait_socket(socket_path)
    log = _Log(Path(config["run_dir"]) / "logs" / f"{arguments.slot}.log")
    worker = Worker(
        JournalClient(socket_path),
        config["run_id"],
        arguments.slot,
        model=config.get("model"),
        log=log,
    )
    state = worker.run()
    log(f"slot stopped: {state}")
    return 0 if state == "complete" else 1


def command_observe(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    allowed = {"orchestrator"} | {f"worker-{i}" for i in range(int(config["workers"]))}
    if arguments.slot not in allowed:
        raise ControllerError(f"unknown slot {arguments.slot!r}; choose one of {sorted(allowed)}")
    path = Path(config["run_dir"]) / "logs" / f"{arguments.slot}.log"
    while not path.exists():
        if arguments.no_follow:
            raise ControllerError(f"slot log has not started: {path}")
        time.sleep(0.1)
    with path.open("r", encoding="utf-8") as stream:
        while True:
            line = stream.readline()
            if line:
                print(line, end="", flush=True)
                continue
            if arguments.no_follow:
                return 0
            try:
                state = JournalClient(config["socket"], timeout=1).run_status(config["run_id"])[
                    "run"
                ]["state"]
            except (JournalError, OSError, ProtocolError, TimeoutError):
                state = "running"
            if state in {"complete", "failed", "stopped"}:
                time.sleep(0.2)
                remainder = stream.read()
                if remainder:
                    print(remainder, end="", flush=True)
                return 0 if state == "complete" else 1
            time.sleep(0.2)


def command_status(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    _wait_socket(Path(config["socket"]), timeout=2)
    status = JournalClient(config["socket"]).run_status(config["run_id"])
    print(json.dumps(status, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="swarmctl")
    root = parser.add_subparsers(dest="group", required=True)
    harness = root.add_parser("harness")
    commands = harness.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--workload", required=True)
    run.add_argument("--target", default=str(ROOT.parent / "memory"))
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--model")
    run.add_argument("--foreground", action="store_true")
    run.add_argument("--resume", action="store_true")
    run.set_defaults(handler=command_run)
    orchestrate = commands.add_parser("orchestrate", help=argparse.SUPPRESS)
    orchestrate.add_argument("--workload", required=True)
    orchestrate.set_defaults(handler=command_orchestrate)
    worker = commands.add_parser("worker", help=argparse.SUPPRESS)
    worker.add_argument("--workload", required=True)
    worker.add_argument("--slot", required=True)
    worker.set_defaults(handler=command_worker)
    observe = commands.add_parser("observe")
    observe.add_argument("--workload", required=True)
    observe.add_argument("--slot", required=True)
    observe.add_argument("--no-follow", action="store_true")
    observe.set_defaults(handler=command_observe)
    status = commands.add_parser("status")
    status.add_argument("--workload", required=True)
    status.set_defaults(handler=command_status)
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        return int(arguments.handler(arguments))
    except (ControllerError, OSError, ValueError) as error:
        print(f"swarmctl: {error}", file=sys.stderr)
        return 2
