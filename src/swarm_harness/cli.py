"""Native macOS command line for running and observing the swarm harness."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

from pygments import highlight as pygments_highlight
from pygments.formatters import TerminalFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

from .controller import Controller, ControllerError, load_stage
from .journal_client import JournalClient
from .live_observer import (
    JournalSlotFollower,
    ObserverSlot,
)
from .live_observer import _render_line as render_observer_line
from .sqlite_service import serve_in_thread
from .worker import Worker

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / ".swarm" / "runs"
ANSI = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}
TIMESTAMP = re.compile(r"^(\[\d{2}:\d{2}:\d{2}\]) (.*)$")
QUEUE_MAX_COLUMNS = 40


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


def _persisted_run_state(config: dict) -> str:
    try:
        uri = f"file:{Path(config['database']).resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.2) as database:
            row = database.execute(
                "SELECT state FROM runs WHERE run_id = ?", (config["run_id"],)
            ).fetchone()
        return str(row[0]) if row else "running"
    except sqlite3.Error:
        return "running"


def _queue_snapshot(config: dict) -> dict | None:
    try:
        uri = f"file:{Path(config['database']).resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.2) as database:
            database.row_factory = sqlite3.Row
            run = database.execute(
                "SELECT run_id,state FROM runs WHERE run_id=?", (config["run_id"],)
            ).fetchone()
            if run is None:
                return None
            tasks = database.execute(
                """
                SELECT t.task_id,t.title,t.state,t.branch,t.accepted_commit,t.last_error,
                  c.worker_id,c.claimed_at,c.expires_at,c.state AS claim_state,
                  c.submission_json,
                  (SELECT COUNT(*) FROM dependencies d JOIN tasks parent
                     ON parent.run_id=d.run_id AND parent.task_id=d.dependency_id
                   WHERE d.run_id=t.run_id AND d.task_id=t.task_id
                     AND parent.state!='accepted') AS blocked_count
                FROM tasks t
                LEFT JOIN claims c ON c.claim_id=(
                  SELECT latest.claim_id FROM claims latest
                  WHERE latest.run_id=t.run_id AND latest.task_id=t.task_id
                  ORDER BY latest.claim_id DESC LIMIT 1
                )
                WHERE t.run_id=? ORDER BY t.ordinal
                """,
                (config["run_id"],),
            ).fetchall()
        return {
            "run_id": run["run_id"],
            "state": run["state"],
            "tasks": [dict(task) for task in tasks],
        }
    except sqlite3.Error:
        return None


def _queue_task_state(task: dict, now: float) -> str:
    state = str(task["state"])
    if state == "accepted":
        return "ACCEPTED"
    if state == "submitted":
        return "VERIFYING"
    if state == "claimed":
        expires = task.get("expires_at")
        if (
            task.get("claim_state") == "active"
            and expires is not None
            and float(expires) <= now
        ):
            return "EXPIRED"
        return "WORKING"
    if state == "pending" and task.get("last_error"):
        return "RETRY"
    if state == "pending" and int(task.get("blocked_count") or 0):
        return "BLOCKED"
    if state == "pending":
        return "READY"
    return state.upper()


def _queue_duration(seconds: float) -> str:
    remaining = max(0, int(seconds))
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m {seconds:02d}s"


def _queue_commit(task: dict) -> str | None:
    commit = task.get("accepted_commit")
    if commit:
        return str(commit)[:12]
    raw = task.get("submission_json")
    if raw:
        try:
            value = json.loads(str(raw))
        except json.JSONDecodeError:
            return None
        commit = value.get("commit_sha") if isinstance(value, dict) else None
        if commit:
            return str(commit)[:12]
    return None


def _render_queue(snapshot: dict | None, *, color: bool, width: int = 40) -> str:
    width = max(12, min(QUEUE_MAX_COLUMNS, width))
    now = time.time()
    lines: list[str] = []

    def add(value: object = "", code: str | None = None, prefix: str = "") -> None:
        wrapped = textwrap.wrap(
            str(value),
            width=width,
            initial_indent=prefix,
            subsequent_indent="  " if prefix else "",
            break_long_words=True,
            break_on_hyphens=False,
        ) or [""]
        lines.extend(_style(line, code, color) if code else line for line in wrapped)

    add("SWARM QUEUE", "1;36")
    if snapshot is None:
        add("WAITING FOR JOURNAL", "1;33")
        return "\n".join(lines) + "\n"
    add(snapshot["run_id"], "2")
    run_state = str(snapshot["state"]).upper()
    add(run_state, "1;32" if run_state == "COMPLETE" else "1;35")
    add(time.strftime("UPDATED %H:%M:%S"), "2")
    add("-" * width, "2;36")
    counts: dict[str, int] = {}
    for task in snapshot["tasks"]:
        state = _queue_task_state(task, now)
        counts[state] = counts.get(state, 0) + 1
        state_color = {
            "ACCEPTED": "1;32",
            "VERIFYING": "1;33",
            "WORKING": "1;35",
            "READY": "1;36",
            "BLOCKED": "2;37",
            "RETRY": "1;31",
            "EXPIRED": "1;31",
        }.get(state, "1;37")
        add(state, state_color)
        add(task["task_id"], "1;37")
        if state in {"WORKING", "EXPIRED"}:
            add(task.get("worker_id") or "unassigned", prefix="owner: ")
            expires = task.get("expires_at")
            lease = "unknown" if expires is None else _queue_duration(float(expires) - now)
            add("expired" if state == "EXPIRED" else lease, prefix="lease: ")
        if state == "BLOCKED":
            add(str(int(task["blocked_count"])), prefix="waiting on: ")
        commit = _queue_commit(task)
        if commit and state in {"VERIFYING", "ACCEPTED"}:
            add(commit, prefix="commit: ")
        if state == "RETRY":
            add(task.get("last_error") or "rejected", prefix="error: ")
        add("-" * width, "2")
    add("SUMMARY", "1;36")
    for state in ("WORKING", "VERIFYING", "READY", "BLOCKED", "RETRY", "EXPIRED", "ACCEPTED"):
        if counts.get(state):
            add(str(counts[state]), prefix=f"{state.lower()}: ")
    return "\n".join(lines) + "\n"


def _paint(text: str, color: str, enabled: bool) -> str:
    return f"{ANSI[color]}{text}{ANSI['reset']}" if enabled else text


def _style(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def _safe(value: object) -> str:
    text = str(value)
    return "".join(
        character
        if character in "\n\t" or ord(character) >= 32 and ord(character) != 127
        else f"\\x{ord(character):02x}"
        for character in text
    )


def _indent(value: str) -> str:
    return "\n".join(f"  {line}" for line in value.splitlines())


def _code(value: object, language: str, color: bool) -> str:
    safe = _safe(value).rstrip("\n")
    if not color or not safe:
        return safe
    try:
        lexer = get_lexer_by_name(language) if language else guess_lexer(safe)
        rendered = pygments_highlight(safe, lexer, TerminalFormatter())
        return rendered.removesuffix("\n")
    except ClassNotFound:
        return safe


def _pretty(value: object, color: bool) -> str:
    if isinstance(value, str):
        return _safe(value)
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
    return _code(rendered, "json", color)


def _status_color(status: object) -> str:
    normalized = str(status).casefold()
    if normalized in {"completed", "success", "succeeded"}:
        return "32"
    if normalized in {"failed", "error", "cancelled"}:
        return "31"
    return "33"


def _render_item(phase: str, item: dict, color: bool) -> str:
    kind = str(item.get("type", "item"))
    if kind in {"reasoning", "agent_message"}:
        label = "REASONING" if kind == "reasoning" else "AGENT"
        label_color = "1;35" if kind == "reasoning" else "1;32"
        text = item.get("text", item.get("content", ""))
        return f"{_style(label, label_color, color)} [{_style(phase, '2', color)}]\n{_indent(_code(text, 'markdown', color))}"
    if kind in {"command_execution", "command"}:
        command = item.get("command", item.get("cmd", ""))
        output = item.get("aggregated_output", item.get("output", item.get("result", "")))
        status, exit_code = item.get("status", ""), item.get("exit_code")
        heading = [_style("COMMAND", "1;36", color), _style(phase, "2", color)]
        if status:
            heading.append(_style(f"status={_safe(status)}", _status_color(status), color))
        if exit_code is not None:
            heading.append(_style(f"exit={_safe(exit_code)}", "32" if exit_code == 0 else "31", color))
        parts = [" ".join(heading), f"{_style('command', '1;34', color)}:\n{_indent(_code(command, 'bash', color))}"]
        if output:
            language = "diff" if "git diff" in str(command) else ""
            parts.append(f"{_style('output', '1;34', color)}:\n{_indent(_code(output, language, color))}")
        return "\n".join(parts)
    if kind in {"mcp_tool_call", "tool_call"}:
        call = f"{item.get('server', '')}.{item.get('tool', item.get('name', ''))}".strip(".")
        heading = f"{_style('TOOL', '1;36', color)} {_style(phase, '2', color)} {_style(call, '1;34', color)}"
        parts = [heading, f"{_style('input', '1;34', color)}:\n{_indent(_pretty(item.get('arguments', item.get('input', '')), color))}"]
        result = item.get("result", item.get("output"))
        if result not in (None, "", {}, []):
            parts.append(f"{_style('output', '1;34', color)}:\n{_indent(_pretty(result, color))}")
        return "\n".join(parts)
    if kind == "file_change":
        heading = f"{_style('FILES', '1;36', color)} {_style(phase, '2', color)}"
        changes = [
            f"  {_style(_safe(change.get('kind', 'change')), '33', color)} {_style(_safe(change.get('path', '')), '1;36', color)}"
            for change in item.get("changes", [])
            if isinstance(change, dict)
        ]
        return "\n".join([heading, *changes])
    return f"{_style('ITEM', '1;35', color)} {_style(phase, '2', color)} type={_style(kind, '1;34', color)}\n{_indent(_pretty(item, color))}"


def _render_event(event: dict, color: bool) -> str:
    event_type = event.get("type")
    if event_type == "thread.started":
        return f"{_style('THREAD START', '1;36', color)} {_style(_safe(event.get('thread_id', '')), '1;33', color)}"
    if event_type == "turn.started":
        return _style("TURN START", "1;36", color)
    if event_type == "turn.completed":
        return _style("TURN COMPLETE", "1;32", color)
    if event_type in {"turn.failed", "error"}:
        detail = event.get("error", event.get("message", event))
        return f"{_style(str(event_type).upper(), '1;31', color)}\n{_indent(_pretty(detail, color))}"
    if event_type in {"item.started", "item.completed", "item.updated"}:
        item = event.get("item")
        if isinstance(item, dict):
            return _render_item(str(event_type).split(".", 1)[1].upper(), item, color)
    return f"{_style('EVENT', '1;35', color)} {_style(_safe(event_type or 'unknown'), '1;34', color)}\n{_indent(_pretty(event, color))}"


def highlight_stream_line(line: str, *, color: bool, raw: bool = False) -> str:
    """Render one timestamped log record with the original observer renderer."""

    if raw:
        return line
    match = TIMESTAMP.match(line)
    timestamp, body = (match.group(1), match.group(2)) if match else ("", line)
    rendered = render_observer_line("stdout", body, color=color).rstrip("\n")
    if not timestamp:
        return rendered
    prefix = _paint(timestamp, "dim", color) + " "
    return prefix + rendered.replace("\n", "\n" + " " * (len(timestamp) + 1))


def _observer_color(mode: str) -> bool:
    if mode == "always":
        return True
    if mode == "never":
        return False
    return sys.stdout.isatty()


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
    print(f"Queue:   ./swarmctl harness observe-queue --workload {arguments.workload}")
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
    color = _observer_color(getattr(arguments, "color", "auto"))
    no_follow = bool(arguments.no_follow)
    if getattr(arguments, "raw", False):
        while not path.exists():
            if no_follow:
                raise ControllerError(f"slot log has not started: {path}")
            time.sleep(0.1)
        with path.open("r", encoding="utf-8") as stream:
            while True:
                line = stream.readline()
                if line:
                    print(line, end="", flush=True)
                    continue
                state = _persisted_run_state(config)
                if no_follow or state in {"complete", "failed", "stopped"}:
                    return 0 if state != "failed" else 1
                time.sleep(0.2)

    slot = ObserverSlot.parse(arguments.slot)
    stop = threading.Event()
    try:
        uri = f"file:{Path(config['database']).resolve()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=0.2) as database:
            row = database.execute(
                "SELECT task_id FROM tasks WHERE run_id = ? ORDER BY ordinal LIMIT 1",
                (config["run_id"],),
            ).fetchone()
        task_id = str(row[0]) if row else None
    except sqlite3.Error:
        task_id = None

    def load_rows(selected: ObserverSlot, after: int, limit: int) -> list[dict]:
        rows: list[dict] = []
        if after < 1:
            rows.append(
                {
                    "event_id": 1,
                    "observer_slot": selected.index,
                    "invocation_id": f"{config['run_id']}:{selected.actor}",
                    "stream_kind": "lifecycle",
                    "kind": "start",
                    "payload": b"",
                    "actor_kind": "orchestrator" if selected.index == 0 else "worker",
                    "task_id": task_id,
                    "role": "orchestrator" if selected.index == 0 else "worker",
                }
            )
        if path.is_file():
            for event_id, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 2
            ):
                if event_id <= after:
                    continue
                match = TIMESTAMP.match(line)
                body = match.group(2) if match else line
                rows.append(
                    {
                        "event_id": event_id,
                        "observer_slot": selected.index,
                        "invocation_id": f"{config['run_id']}:{selected.actor}",
                        "stream_kind": "stdout",
                        "kind": "chunk",
                        "payload": body + "\n",
                    }
                )
                if len(rows) >= limit:
                    break
        if not rows and _persisted_run_state(config) in {"complete", "failed", "stopped"}:
            stop.set()
        return rows

    def load_state(_selected: ObserverSlot) -> str:
        state = _persisted_run_state(config)
        if state == "complete":
            return "COMPLETE"
        if state in {"failed", "stopped"}:
            return "FAILED"
        return "ACTIVE" if path.exists() else "WAITING"

    JournalSlotFollower(
        slot,
        load_rows,
        load_state=load_state,
        color=color,
    ).run(follow=not no_follow, stop_event=stop)
    return 1 if _persisted_run_state(config) == "failed" else 0


def command_observe_queue(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    color = _observer_color(getattr(arguments, "color", "auto"))
    no_follow = bool(getattr(arguments, "no_follow", False))
    terminal_width = shutil.get_terminal_size(fallback=(QUEUE_MAX_COLUMNS, 24)).columns
    width = max(20, min(QUEUE_MAX_COLUMNS, terminal_width))
    interactive = sys.stdout.isatty() and not no_follow
    cursor_hidden = False
    last_rendered: str | None = None

    try:
        while True:
            snapshot = _queue_snapshot(config)
            rendered = _render_queue(snapshot, color=color, width=width)
            if interactive:
                if not cursor_hidden:
                    sys.stdout.write("\033[?25l")
                    cursor_hidden = True
                sys.stdout.write("\033[H\033[2J" + rendered)
                sys.stdout.flush()
            elif rendered != last_rendered:
                sys.stdout.write(rendered)
                sys.stdout.flush()
            last_rendered = rendered

            if no_follow:
                return 0
            state = str(snapshot["state"]) if snapshot is not None else "running"
            if state in {"complete", "failed", "stopped"}:
                return 0 if state == "complete" else 1
            time.sleep(1)
    finally:
        if cursor_hidden:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()


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
    observe.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    observe.add_argument("--raw", action="store_true")
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
    except (ControllerError, OSError, ValueError) as error:
        print(f"swarmctl: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
