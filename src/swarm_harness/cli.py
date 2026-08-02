"""Native macOS command line for running and observing the swarm harness."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from collections.abc import Callable
from pathlib import Path

from pygments import highlight as pygments_highlight
from pygments.formatters import TerminalFormatter
from pygments.lexers import get_lexer_by_name, guess_lexer
from pygments.util import ClassNotFound

from .controller import ControllerError, Launcher, load_stage
from .journal_client import JournalClient
from .live_observer import (
    JournalSlotFollower,
    ObserverSlot,
)
from .live_observer import _render_line as render_observer_line
from .protocol import JournalError
from .sqlite_service import SQLiteJournal, serve_in_thread
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
            try:
                print(line, flush=True)
            except BrokenPipeError:
                # The persisted observer log survives a disposable detached terminal.
                pass


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
            claim_columns = {
                str(row[1]) for row in database.execute("PRAGMA table_info(claims)")
            }
            ownership_mode = (
                "c.ownership_mode" if "ownership_mode" in claim_columns else "'lease'"
            )
            tasks = database.execute(
                f"""
                SELECT t.task_id,t.title,t.state,t.branch,t.accepted_commit,t.last_error,
                  c.worker_id,c.claimed_at,c.expires_at,c.state AS claim_state,
                  c.submission_json,{ownership_mode} AS ownership_mode,
                  p.proposal_id,p.kind AS proposal_kind,p.state AS proposal_state,
                  p.required_votes,
                  (SELECT COUNT(*) FROM proposal_votes votes
                   WHERE votes.proposal_id=p.proposal_id AND votes.vote='approve')
                    AS approvals,
                  (SELECT COUNT(*) FROM proposal_votes votes
                   WHERE votes.proposal_id=p.proposal_id AND votes.vote='reject')
                    AS rejections,
                  (SELECT GROUP_CONCAT(active.worker_id, ',')
                   FROM validation_claims active
                   WHERE active.proposal_id=p.proposal_id AND active.state='active')
                    AS validators,
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
                LEFT JOIN proposals p ON p.proposal_id=(
                  SELECT latest.proposal_id FROM proposals latest
                  WHERE latest.run_id=t.run_id AND latest.task_id=t.task_id
                  ORDER BY latest.proposal_id DESC LIMIT 1
                )
                WHERE t.run_id=? ORDER BY t.ordinal
                """,
                (config["run_id"],),
            ).fetchall()
            decisions = database.execute(
                """
                SELECT p.proposal_id,p.kind,p.state,p.required_votes,
                  (SELECT COUNT(*) FROM proposal_votes v
                   WHERE v.proposal_id=p.proposal_id AND v.vote='approve') approvals,
                  (SELECT COUNT(*) FROM proposal_votes v
                   WHERE v.proposal_id=p.proposal_id AND v.vote='reject') rejections
                FROM proposals p
                WHERE p.run_id=? AND p.task_id IS NULL
                  AND p.state IN ('open','committed')
                ORDER BY p.proposal_id
                """,
                (config["run_id"],),
            ).fetchall()
        return {
            "run_id": run["run_id"],
            "state": run["state"],
            "tasks": [dict(task) for task in tasks],
            "decisions": [dict(decision) for decision in decisions],
        }
    except sqlite3.Error:
        return None


def _queue_task_state(task: dict, now: float) -> str:
    state = str(task["state"])
    if state == "accepted":
        return "ACCEPTED"
    if state == "submitted":
        return "VERIFYING"
    if state == "integrating":
        return "INTEGRATING"
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
    visible: list[tuple[int, dict, str]] = []
    priority = {
        "WORKING": 0,
        "VERIFYING": 1,
        "INTEGRATING": 2,
        "EXPIRED": 3,
        "RETRY": 4,
        "READY": 5,
        "ACCEPTED": 6,
    }
    for ordinal, task in enumerate(snapshot["tasks"]):
        state = _queue_task_state(task, now)
        if state == "BLOCKED":
            continue
        visible.append((ordinal, task, state))
    visible.sort(key=lambda item: (priority.get(item[2], 6), item[0]))

    counts: dict[str, int] = {}
    for _, task, state in visible:
        counts[state] = counts.get(state, 0) + 1
        state_color = {
            "ACCEPTED": "1;32",
            "VERIFYING": "1;33",
            "INTEGRATING": "1;33",
            "WORKING": "1;35",
            "READY": "1;36",
            "RETRY": "1;31",
            "EXPIRED": "1;31",
        }.get(state, "1;37")
        add(state, state_color)
        add(task["task_id"], "1;37")
        if state in {"WORKING", "EXPIRED"}:
            add(task.get("worker_id") or "unassigned", prefix="owner: ")
            if task.get("ownership_mode") == "observable":
                add("observed", prefix="liveness: ")
            else:
                expires = task.get("expires_at")
                lease = "unknown" if expires is None else _queue_duration(float(expires) - now)
                add("expired" if state == "EXPIRED" else lease, prefix="lease: ")
        if state in {"VERIFYING", "INTEGRATING"} and task.get("proposal_id"):
            add(f"#{task['proposal_id']}", prefix="proposal: ")
            add(
                f"{task.get('approvals') or 0}/{task.get('required_votes') or 2} yes, "
                f"{task.get('rejections') or 0} no",
                prefix="votes: ",
            )
            if task.get("validators"):
                add(task["validators"], prefix="checking: ")
        commit = _queue_commit(task)
        if commit and state in {"VERIFYING", "INTEGRATING", "ACCEPTED"}:
            add(commit, prefix="commit: ")
        if state == "RETRY":
            add(task.get("last_error") or "rejected", prefix="error: ")
        add("-" * width, "2")
    add("SUMMARY", "1;36")
    for state in (
        "WORKING",
        "VERIFYING",
        "INTEGRATING",
        "READY",
        "RETRY",
        "EXPIRED",
        "ACCEPTED",
    ):
        if counts.get(state):
            add(str(counts[state]), prefix=f"{state.lower()}: ")
    for decision in snapshot.get("decisions", []):
        add("STAGE DECISION", "1;33")
        add(f"#{decision['proposal_id']} {decision['kind']}")
        add(
            f"{decision['approvals']}/{decision['required_votes']} yes, "
            f"{decision['rejections']} no",
            prefix="votes: ",
        )
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


def _process_table() -> list[tuple[int, str]]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,command=", "-ww"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise ControllerError(completed.stderr.strip() or "could not inspect processes")
    rows: list[tuple[int, str]] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=1)
        if len(fields) == 2 and fields[0].isdigit():
            rows.append((int(fields[0]), fields[1]))
    return rows


def _run_processes(config: dict) -> list[tuple[int, str]]:
    workload = str(config["workload"])
    swarmctl = str(ROOT / "swarmctl")
    run_worktrees = str(Path(config["run_dir"]).resolve() / "worktrees")
    rows: list[tuple[int, str]] = []
    for pid, command in _process_table():
        if swarmctl in command and f"harness worker --workload {workload}" in command:
            rows.append((pid, "worker"))
        elif swarmctl in command and (
            f"harness launch --workload {workload}" in command
            or f"harness orchestrate --workload {workload}" in command
        ):
            rows.append((pid, "orchestrator"))
        elif "codex exec -C " in command and run_worktrees in command:
            rows.append((pid, "codex"))
    return rows


def _live_worker_slots(config: dict) -> dict[str, set[int]]:
    workload = re.escape(str(config["workload"]))
    swarmctl = re.escape(str(ROOT / "swarmctl"))
    pattern = re.compile(
        rf"{swarmctl} harness worker --workload {workload} --slot ([^ ]+)"
    )
    slots: dict[str, set[int]] = {}
    for pid, command in _process_table():
        match = pattern.search(command)
        if match:
            slots.setdefault(match.group(1), set()).add(pid)
    return slots


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


def _terminate_orphan_codex(worktrees: list[str]) -> None:
    paths = {str(Path(path).resolve()) for path in worktrees if path}
    if not paths:
        return

    def matching() -> list[int]:
        return [
            pid
            for pid, command in _process_table()
            if "codex exec -C " in command and any(path in command for path in paths)
        ]

    for pid in matching():
        _signal_process(pid, "codex", signal.SIGTERM)
    deadline = time.monotonic() + 2
    remaining = matching()
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = matching()
    for pid in remaining:
        _signal_process(pid, "codex", signal.SIGKILL)
    deadline = time.monotonic() + 1
    remaining = matching()
    while remaining and time.monotonic() < deadline:
        time.sleep(0.05)
        remaining = matching()
    if remaining:
        raise ControllerError(
            "orphaned Codex process did not stop: "
            + ", ".join(str(pid) for pid in remaining)
        )


class _LocalWorkerSupervisor:
    def __init__(
        self,
        config: dict,
        client: JournalClient,
        log: Callable[[str], None],
        *,
        launch_timeout: float = 5.0,
    ) -> None:
        self.config = config
        self.client = client
        self.log = log
        self.launch_timeout = launch_timeout
        self.expected = {f"worker-{index}" for index in range(int(config["workers"]))}
        self.alive: set[str] = set()
        self.starting: dict[str, float] = {}

    def _launch(self, slot: str) -> None:
        _launch_terminal(_worker_argv(str(self.config["workload"]), slot))
        self.starting[slot] = time.monotonic() + self.launch_timeout
        self.log(f"launched {slot}")

    def _recover(self, slot: str, reason: str) -> None:
        result = self.client.call(
            "reclaim_worker",
            run_id=self.config["run_id"],
            worker_id=slot,
            reason=reason,
        )
        reclaimed = list(result["reclaimed"])
        _terminate_orphan_codex(
            [str(item.get("worktree_path") or "") for item in reclaimed]
        )
        self.alive.discard(slot)
        self.starting.pop(slot, None)
        if result["run_state"] != "running":
            return
        if reclaimed:
            tasks = ", ".join(str(item["task_id"]) for item in reclaimed)
            self.log(f"reclaimed {tasks} after {slot} process loss")
        self._launch(slot)

    def start(self) -> None:
        current = set(_live_worker_slots(self.config)) & self.expected
        self.alive = set(current)
        for slot in sorted(self.expected - current):
            self._recover(slot, "controller_start_observed_worker_absent")
        for slot in sorted(current):
            self.log(f"adopted live {slot}")

    def tick(self) -> None:
        current = set(_live_worker_slots(self.config)) & self.expected
        for slot in current:
            self.alive.add(slot)
            self.starting.pop(slot, None)
        for slot in sorted(self.alive - current):
            self._recover(slot, "worker_process_exited")
        now = time.monotonic()
        for slot, deadline in sorted(self.starting.items()):
            if slot not in current and now >= deadline:
                self._recover(slot, "worker_launch_not_observed")


def _signal_process(pid: int, kind: str, signal_number: int) -> None:
    try:
        if kind == "codex":
            os.killpg(pid, signal_number)
        else:
            os.kill(pid, signal_number)
    except ProcessLookupError:
        pass
    except PermissionError as error:
        raise ControllerError(f"cannot signal {kind} process {pid}") from error


def _wait_for_process_drain(config: dict, timeout: float) -> list[tuple[int, str]]:
    deadline = time.monotonic() + timeout
    while True:
        rows = _run_processes(config)
        if not rows or time.monotonic() >= deadline:
            return rows
        time.sleep(0.1)


def _stop_run_processes(config: dict) -> None:
    rows = _run_processes(config)
    for kind in ("worker", "orchestrator"):
        for pid, process_kind in rows:
            if process_kind == kind:
                _signal_process(pid, kind, signal.SIGINT)
    remaining = _wait_for_process_drain(config, 10)
    for pid, kind in remaining:
        _signal_process(pid, kind, signal.SIGTERM)
    remaining = _wait_for_process_drain(config, 5)
    for pid, kind in remaining:
        _signal_process(pid, kind, signal.SIGKILL)
    remaining = _wait_for_process_drain(config, 5)
    if remaining:
        detail = ", ".join(f"{kind}:{pid}" for pid, kind in remaining)
        raise ControllerError(f"run processes did not stop: {detail}")


def _start_run(config: dict, *, foreground: bool) -> int:
    workload = str(config["workload"])
    argv = [str(ROOT / "swarmctl"), "harness", "launch", "--workload", workload]
    if foreground:
        return command_launch(argparse.Namespace(workload=workload))
    _launch_terminal(argv)
    print(f"Started {workload} in a native Terminal window.")
    print(f"Observe: ./swarmctl harness observe --workload {workload} --slot orchestrator")
    print(f"Worker:  ./swarmctl harness observe --workload {workload} --slot worker-0")
    print(f"Queue:   ./swarmctl harness observe-queue --workload {workload}")
    return 0


def _resume_run(workload: str, *, foreground: bool) -> int:
    config = _load_config(workload)
    processes = _run_processes(config)
    if any(kind == "orchestrator" for _, kind in processes):
        raise ControllerError(f"{workload} already has a live launcher")
    try:
        _offline_journal(config).op_resume_run({"run_id": config["run_id"]})
    except JournalError as error:
        raise ControllerError(str(error)) from error
    return _start_run(config, foreground=foreground)


def command_run(arguments: argparse.Namespace) -> int:
    if arguments.resume:
        return _resume_run(arguments.workload, foreground=arguments.foreground)
    stage = load_stage(_stage_path(arguments.workload))
    target = Path(arguments.target).expanduser().resolve()
    run_dir = _run_dir(arguments.workload)
    config_path = _config_path(arguments.workload)
    if config_path.exists():
        raise ControllerError(
            f"run already exists at {run_dir}; use resume or reset the workload"
        )
    if arguments.workers < 1:
        raise ControllerError("workers must be positive")
    config = {
        "schema_version": 1,
        "run_id": f"{arguments.workload}-{time.strftime('%Y%m%d-%H%M%S')}",
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
    return _start_run(config, foreground=arguments.foreground)


def command_launch(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    run_dir = Path(config["run_dir"])
    log = _Log(run_dir / "logs" / "orchestrator.log")
    server, thread = serve_in_thread(config["database"], config["socket"])
    client = JournalClient(config["socket"])
    stage = load_stage(config["stage_path"])
    launcher = Launcher(
        client,
        config["run_id"],
        config["workload"],
        stage,
        config["target_repo"],
        run_dir,
        log,
    )
    try:
        launcher.seed()
        initial_state = client.run_status(config["run_id"])["run"]["state"]
        if initial_state != "running":
            return 0 if initial_state in {"paused", "stopped", "complete"} else 1
        launcher.prepare_runnable()
        supervisor = _LocalWorkerSupervisor(config, client, log)
        supervisor.start()
        while True:
            supervisor.tick()
            state = launcher.tick()
            if state in {"complete", "paused", "failed", "stopped"}:
                return 0 if state in {"complete", "paused", "stopped"} else 1
            time.sleep(0.5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# Preserve old private command lines during a rolling upgrade.
command_orchestrate = command_launch


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
    return 0 if state in {"complete", "paused", "stopped"} else 1


def _offline_journal(config: dict) -> SQLiteJournal:
    database = Path(config["database"])
    if not database.is_file():
        raise ControllerError(f"run journal is missing: {database}")
    return SQLiteJournal(database)


def _pause_run(config: dict) -> dict:
    state = _persisted_run_state(config)
    if state in {"complete", "failed"}:
        _stop_run_processes(config)
        return {"state": state, "paused_claims": 0}
    journal = _offline_journal(config)
    try:
        before_stop = journal.op_pause_run({"run_id": config["run_id"]})
        _stop_run_processes(config)
        after_stop = journal.op_pause_run({"run_id": config["run_id"]})
        return {
            "state": after_stop["state"],
            "paused_claims": before_stop["paused_claims"] + after_stop["paused_claims"],
        }
    except JournalError as error:
        raise ControllerError(str(error)) from error


def command_pause(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    result = _pause_run(config)
    print(
        f"{arguments.workload}: {result['state']} "
        f"({result['paused_claims']} active claim(s) fenced)"
    )
    return 0


def command_resume(arguments: argparse.Namespace) -> int:
    return _resume_run(arguments.workload, foreground=arguments.foreground)


def _git_run(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode:
        raise ControllerError(completed.stderr.strip() or "Git command failed")
    return completed


def _remove_run_worktrees(config: dict) -> int:
    repository = Path(config["target_repo"]).resolve()
    run_dir = Path(config["run_dir"]).resolve()
    removed = 0
    rows = _git_run(repository, "worktree", "list", "--porcelain").stdout.splitlines()
    for line in rows:
        if not line.startswith("worktree "):
            continue
        worktree = Path(line.removeprefix("worktree ")).resolve()
        try:
            relative = worktree.relative_to(run_dir)
        except ValueError:
            continue
        if not relative.parts or relative.parts[0] not in {"integration", "worktrees"}:
            raise ControllerError(f"refusing to reset unexpected worktree: {worktree}")
        _git_run(repository, "worktree", "remove", "--force", str(worktree))
        removed += 1
    _git_run(repository, "worktree", "prune")
    return removed


def _delete_run_branches(config: dict) -> int:
    repository = Path(config["target_repo"]).resolve()
    run_slug = re.sub(r"[^a-zA-Z0-9]+", "-", str(config["run_id"])).strip("-").lower()
    prefix = f"refs/heads/codex/swarm-{run_slug}/"
    output = _git_run(
        repository,
        "for-each-ref",
        "--format=%(refname)",
        prefix,
    ).stdout
    refs = [ref for ref in output.splitlines() if ref]
    for ref in refs:
        if not ref.startswith(prefix):
            raise ControllerError(f"refusing to reset unexpected branch: {ref}")
        _git_run(repository, "update-ref", "-d", ref)
    return len(refs)


def command_reset(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    configured_run_dir = Path(config["run_dir"])
    expected_run_dir = _run_dir(arguments.workload)
    if (
        configured_run_dir.is_symlink()
        or configured_run_dir.resolve() != expected_run_dir.resolve()
    ):
        raise ControllerError("run directory does not match the selected workload")
    _pause_run(config)
    worktree_count = _remove_run_worktrees(config)
    branch_count = _delete_run_branches(config)
    archive_root = RUNS.parent / "archive"
    archive_root.mkdir(parents=True, exist_ok=True)
    destination = archive_root / f"{arguments.workload}-{config['run_id']}"
    if destination.exists():
        destination = archive_root / (
            f"{arguments.workload}-{config['run_id']}-{time.time_ns()}"
        )
    configured_run_dir.replace(destination)
    print(f"Reset {arguments.workload}; archived prior state at {destination}")
    print(f"Removed {worktree_count} worktree(s) and {branch_count} run branch(es).")
    return 0


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
                if no_follow or state in {"complete", "paused", "failed", "stopped"}:
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
        if not rows and _persisted_run_state(config) in {
            "complete",
            "paused",
            "failed",
            "stopped",
        }:
            stop.set()
        return rows

    def load_state(_selected: ObserverSlot) -> str:
        state = _persisted_run_state(config)
        if state == "complete":
            return "COMPLETE"
        if state == "paused":
            return "PAUSED"
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
            if state in {"complete", "paused", "failed", "stopped"}:
                return 0 if state in {"complete", "paused", "stopped"} else 1
            time.sleep(1)
    finally:
        if cursor_hidden:
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()


def command_status(arguments: argparse.Namespace) -> int:
    config = _load_config(arguments.workload)
    socket_path = Path(config["socket"])
    if socket_path.exists():
        try:
            status = JournalClient(socket_path, timeout=0.5).run_status(config["run_id"])
        except OSError:
            status = _offline_journal(config).op_run_status({"run_id": config["run_id"]})
    else:
        status = _offline_journal(config).op_run_status({"run_id": config["run_id"]})
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
    pause = commands.add_parser("pause")
    pause.add_argument("--workload", required=True)
    pause.set_defaults(handler=command_pause)
    resume = commands.add_parser("resume")
    resume.add_argument("--workload", required=True)
    resume.add_argument("--foreground", action="store_true")
    resume.set_defaults(handler=command_resume)
    reset = commands.add_parser("reset")
    reset.add_argument("--workload", required=True)
    reset.set_defaults(handler=command_reset)
    launch = commands.add_parser("launch", help=argparse.SUPPRESS)
    launch.add_argument("--workload", required=True)
    launch.set_defaults(handler=command_launch)
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
    except (ControllerError, JournalError, OSError, ValueError) as error:
        print(f"swarmctl: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
