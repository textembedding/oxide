"""Swappable two-operation journal prototype.

All task coordination lives here.  Callers can only add text or search the
journal; SQLite and the queue projection are implementation details that the
future Rust MCP server may replace wholesale.
"""

from __future__ import annotations

import json
import re
import secrets
import socket
import socketserver
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any


class JournalError(RuntimeError):
    pass


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  state TEXT NOT NULL,
  stage_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  title TEXT NOT NULL,
  prompt TEXT NOT NULL,
  dependencies_json TEXT NOT NULL,
  checks_json TEXT NOT NULL,
  state TEXT NOT NULL,
  worker_id TEXT,
  commit_sha TEXT,
  updated_at REAL NOT NULL,
  PRIMARY KEY (run_id, task_id),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS entries (
  journal_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  worker_id TEXT NOT NULL,
  body TEXT NOT NULL,
  created_at REAL NOT NULL,
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
"""

_TASK_MARKER = re.compile(r"^(claim|checkpoint|handoff|complete): task:([^\s]+)$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _line_value(text: str, name: str) -> str | None:
    prefix = name + ":"
    for line in text.splitlines()[1:]:
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    return None


class Journal:
    """Reference prototype behind the exact two-tool boundary."""

    def __init__(self, database: str | Path, clock=time.time) -> None:
        self.database = Path(database)
        self.clock = clock
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            existing = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if existing and "entries" not in existing:
                raise JournalError("legacy journal must be archived before this prototype starts")
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _require(value: object, name: str) -> str:
        if not isinstance(value, str) or not value:
            raise JournalError(f"{name} must be a nonempty string")
        return value

    def dispatch(self, operation: str, arguments: dict[str, Any]) -> Any:
        if operation == "journal_add":
            return self.add(
                self._require(arguments.get("run_id"), "run_id"),
                self._require(arguments.get("worker_id"), "worker_id"),
                self._require(arguments.get("text"), "text"),
            )
        if operation == "journal_search":
            return self.search(
                self._require(arguments.get("run_id"), "run_id"),
                self._require(arguments.get("query"), "query"),
            )
        raise JournalError("the journal exposes only journal_add and journal_search")

    def _append(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        worker_id: str,
        body: str,
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO entries(run_id,worker_id,body,created_at) VALUES(?,?,?,?)",
            (run_id, worker_id, body, self.clock()),
        )
        return int(cursor.lastrowid)

    def _bootstrap(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        worker_id: str,
        text: str,
    ) -> dict[str, Any]:
        if worker_id != "launcher" or text.splitlines()[0] != f"bootstrap: run:{run_id}":
            raise JournalError("only the launcher may bootstrap its exact run")
        raw_stage = _line_value(text, "stage-json")
        if raw_stage is None:
            raise JournalError("bootstrap requires stage-json")
        try:
            stage = json.loads(raw_stage)
        except json.JSONDecodeError as error:
            raise JournalError("stage-json is invalid") from error
        tasks = stage.get("tasks") if isinstance(stage, dict) else None
        if not isinstance(tasks, list) or not tasks:
            raise JournalError("stage requires tasks")
        stored = connection.execute(
            "SELECT stage_json,state FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        canonical = _compact(stage)
        if stored is not None:
            if stored["stage_json"] != canonical:
                raise JournalError("run already exists with different stage bytes")
            return {"saved": False, "state": stored["state"]}
        identifiers = {str(task.get("id", "")) for task in tasks if isinstance(task, dict)}
        if "" in identifiers or len(identifiers) != len(tasks):
            raise JournalError("task identifiers must be unique and nonempty")
        for task in tasks:
            dependencies = task.get("depends_on", [])
            checks = task.get("checks", [])
            if (
                not isinstance(dependencies, list)
                or not all(isinstance(item, str) and item for item in dependencies)
                or not set(dependencies) <= identifiers
                or not isinstance(checks, list)
                or not all(isinstance(item, str) and item for item in checks)
            ):
                raise JournalError("task dependencies or checks are invalid")
            if task["id"] in dependencies:
                raise JournalError("a task cannot depend on itself")
        remaining = {str(task["id"]): set(task.get("depends_on", [])) for task in tasks}
        while remaining:
            ready = {task_id for task_id, dependencies in remaining.items() if not dependencies}
            if not ready:
                raise JournalError("task dependencies contain a cycle")
            remaining = {
                task_id: dependencies - ready
                for task_id, dependencies in remaining.items()
                if task_id not in ready
            }
        connection.execute(
            "INSERT INTO runs(run_id,state,stage_json,created_at) VALUES(?,'running',?,?)",
            (run_id, canonical, self.clock()),
        )
        bootstrap_id = self._append(connection, run_id, worker_id, text)
        for ordinal, task in enumerate(tasks):
            connection.execute(
                """
                INSERT INTO tasks(
                  run_id,task_id,ordinal,title,prompt,dependencies_json,checks_json,
                  state,worker_id,commit_sha,updated_at
                ) VALUES(?,?,?,?,?,?,?,'pending',NULL,NULL,?)
                """,
                (
                    run_id,
                    task["id"],
                    ordinal,
                    str(task.get("title", task["id"])),
                    str(task.get("prompt", "")),
                    _compact(task.get("depends_on", [])),
                    _compact(task.get("checks", [])),
                    self.clock(),
                ),
            )
            self._append(
                connection,
                run_id,
                "launcher",
                self._task_body(task, "seeded"),
            )
        return {"saved": True, "journal_id": bootstrap_id, "state": "running"}

    @staticmethod
    def _task_body(task: dict[str, Any], state: str) -> str:
        lines = [
            "queue:task",
            f"task:{task['id']}",
            f"state:{state}",
            f"title:{task.get('title', task['id'])}",
            f"objective:{task.get('prompt', '')}",
            f"depends-on:{_compact(task.get('depends_on', []))}",
            f"checks:{_compact(task.get('checks', []))}",
        ]
        return "\n".join(lines)

    @staticmethod
    def _dependencies_complete(
        connection: sqlite3.Connection, run_id: str, dependencies: list[str]
    ) -> bool:
        if not dependencies:
            return True
        placeholders = ",".join("?" for _ in dependencies)
        complete = connection.execute(
            f"SELECT COUNT(*) FROM tasks WHERE run_id=? AND task_id IN ({placeholders}) AND state='complete'",
            (run_id, *dependencies),
        ).fetchone()[0]
        return int(complete) == len(dependencies)

    def _claim(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        worker_id: str,
        task_id: str,
        text: str,
    ) -> dict[str, Any]:
        owned = connection.execute(
            "SELECT task_id FROM tasks WHERE run_id=? AND worker_id=? AND state='claimed'",
            (run_id, worker_id),
        ).fetchone()
        if owned is not None and owned["task_id"] != task_id:
            raise JournalError(f"worker already owns task:{owned['task_id']}")
        task = connection.execute(
            "SELECT * FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
        ).fetchone()
        if task is None:
            raise JournalError("unknown task")
        if task["state"] == "claimed" and task["worker_id"] == worker_id:
            return {"saved": False, "claim": "resumed", "task": self._task_value(task)}
        if task["state"] != "pending":
            raise JournalError(f"task is {task['state']}")
        dependencies = json.loads(task["dependencies_json"])
        if not self._dependencies_complete(connection, run_id, dependencies):
            raise JournalError("task dependencies are not complete")
        changed = connection.execute(
            """
            UPDATE tasks SET state='claimed',worker_id=?,updated_at=?
            WHERE run_id=? AND task_id=? AND state='pending'
            """,
            (worker_id, self.clock(), run_id, task_id),
        ).rowcount
        if changed != 1:
            raise JournalError("task was claimed concurrently")
        journal_id = self._append(connection, run_id, worker_id, text)
        task = connection.execute(
            "SELECT * FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
        ).fetchone()
        return {
            "saved": True,
            "journal_id": journal_id,
            "claim": "accepted",
            "task": self._task_value(task),
        }

    def _complete(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        worker_id: str,
        task_id: str,
        text: str,
    ) -> dict[str, Any]:
        commit = _line_value(text, "commit") or ""
        verified = _line_value(text, "verified")
        if not _COMMIT.fullmatch(commit) or verified != "true":
            raise JournalError("completion requires exact commit and verified:true")
        task = connection.execute(
            "SELECT * FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
        ).fetchone()
        if task is None or task["state"] != "claimed" or task["worker_id"] != worker_id:
            raise JournalError("only the owning worker may complete the task")
        markers = (
            f"checkpoint: task:{task_id}",
            f"handoff: task:{task_id}",
        )
        for marker in markers:
            found = connection.execute(
                """
                SELECT 1 FROM entries
                WHERE run_id=? AND worker_id=?
                  AND (body=? OR substr(body,1,length(?)+1)=? || char(10)) LIMIT 1
                """,
                (run_id, worker_id, marker, marker, marker),
            ).fetchone()
            if found is None:
                raise JournalError(f"completion requires {marker}")
        journal_id = self._append(connection, run_id, worker_id, text)
        connection.execute(
            """
            UPDATE tasks SET state='complete',commit_sha=?,updated_at=?
            WHERE run_id=? AND task_id=?
            """,
            (commit, self.clock(), run_id, task_id),
        )
        remaining = connection.execute(
            "SELECT COUNT(*) FROM tasks WHERE run_id=? AND state!='complete'", (run_id,)
        ).fetchone()[0]
        if int(remaining) == 0:
            connection.execute("UPDATE runs SET state='complete' WHERE run_id=?", (run_id,))
        return {
            "saved": True,
            "journal_id": journal_id,
            "task": task_id,
            "state": "complete",
            "run_complete": int(remaining) == 0,
        }

    def add(self, run_id: str, worker_id: str, text: str) -> dict[str, Any]:
        encoded = text.encode("utf-8")
        if not text.strip() or len(encoded) > 524_288:
            raise JournalError("journal text must be 1..524288 UTF-8 bytes")
        first = text.splitlines()[0]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if first.startswith("bootstrap:"):
                return self._bootstrap(connection, run_id, worker_id, text)
            run = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise JournalError("run does not exist")
            if first in {"control: pause", "control: resume"}:
                if worker_id != "launcher":
                    raise JournalError("only the launcher may change run control state")
                state = "paused" if first.endswith("pause") else "running"
                if run["state"] == "complete":
                    raise JournalError("a complete run cannot change state")
                connection.execute("UPDATE runs SET state=? WHERE run_id=?", (state, run_id))
                journal_id = self._append(connection, run_id, worker_id, text)
                return {"saved": True, "journal_id": journal_id, "state": state}
            if run["state"] != "running":
                raise JournalError(f"run is {run['state']}")
            marker = _TASK_MARKER.fullmatch(first)
            if marker and marker.group(1) == "claim":
                return self._claim(connection, run_id, worker_id, marker.group(2), text)
            if marker and marker.group(1) == "complete":
                return self._complete(connection, run_id, worker_id, marker.group(2), text)
            if marker and marker.group(1) in {"checkpoint", "handoff"}:
                owned = connection.execute(
                    """
                    SELECT 1 FROM tasks
                    WHERE run_id=? AND task_id=? AND worker_id=? AND state='claimed'
                    """,
                    (run_id, marker.group(2), worker_id),
                ).fetchone()
                if owned is None:
                    raise JournalError("journal marker does not belong to this worker")
            journal_id = self._append(connection, run_id, worker_id, text)
        return {"saved": True, "journal_id": journal_id}

    def _task_state(self, connection: sqlite3.Connection, row: sqlite3.Row) -> str:
        if row["state"] != "pending":
            return "working" if row["state"] == "claimed" else str(row["state"])
        dependencies = json.loads(row["dependencies_json"])
        return (
            "ready"
            if self._dependencies_complete(connection, row["run_id"], dependencies)
            else "blocked"
        )

    def _task_value(self, row: sqlite3.Row, state: str | None = None) -> dict[str, Any]:
        value = {
            "task_id": row["task_id"],
            "title": row["title"],
            "prompt": row["prompt"],
            "depends_on": json.loads(row["dependencies_json"]),
            "checks": json.loads(row["checks_json"]),
            "state": state or row["state"],
            "worker_id": row["worker_id"],
            "commit_sha": row["commit_sha"],
        }
        value["body"] = "\n".join(
            [
                "queue:task",
                f"task:{value['task_id']}",
                f"state:{value['state']}",
                f"title:{value['title']}",
                f"objective:{value['prompt']}",
                f"depends-on:{_compact(value['depends_on'])}",
                f"checks:{_compact(value['checks'])}",
                f"worker:{value['worker_id'] or ''}",
                f"commit:{value['commit_sha'] or ''}",
            ]
        )
        value["kind"] = "task"
        return value

    def search(self, run_id: str, query: str) -> list[dict[str, Any]]:
        if not query.strip() or len(query.encode("utf-8")) > 4096:
            raise JournalError("journal query must be 1..4096 UTF-8 bytes")
        with self._connect() as connection:
            run = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise JournalError("run does not exist")
            if query == "run:state":
                return [
                    {
                        "kind": "run",
                        "state": run["state"],
                        "body": f"run:state\nstate:{run['state']}",
                    }
                ]
            if query in {"queue:all", "queue:ready"} or query.startswith("worker:"):
                rows = connection.execute(
                    "SELECT * FROM tasks WHERE run_id=? ORDER BY ordinal", (run_id,)
                ).fetchall()
                values = [self._task_value(row, self._task_state(connection, row)) for row in rows]
                if query == "queue:ready":
                    return [value for value in values if value["state"] == "ready"]
                if query.startswith("worker:"):
                    worker_id = query.removeprefix("worker:")
                    return [
                        value
                        for value in values
                        if value["state"] == "working" and value["worker_id"] == worker_id
                    ]
                return values
            task_id = query.removeprefix("task:") if query.startswith("task:") else None
            matches: list[dict[str, Any]] = []
            if task_id:
                row = connection.execute(
                    "SELECT * FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
                ).fetchone()
                if row is not None:
                    matches.append(self._task_value(row, self._task_state(connection, row)))
            rows = connection.execute(
                """
                SELECT journal_id,worker_id,body,created_at FROM entries
                WHERE run_id=? AND INSTR(body,?)>0
                ORDER BY journal_id DESC LIMIT 100
                """,
                (run_id, query),
            ).fetchall()
            matches.extend({"kind": "entry", **dict(row)} for row in rows)
            return matches[:100]


def _encode(value: object) -> bytes:
    return (_compact(value) + "\n").encode()


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request_id = "unknown"
        try:
            request = json.loads(self.rfile.readline())
            if not isinstance(request, dict):
                raise JournalError("request must be an object")
            request_id = str(request.get("request_id", ""))
            operation = str(request.get("operation", ""))
            arguments = request.get("arguments")
            if not request_id or not isinstance(arguments, dict):
                raise JournalError("request envelope is invalid")
            result = self.server.journal.dispatch(operation, arguments)  # type: ignore[attr-defined]
            response = {"request_id": request_id, "ok": True, "result": result}
        except (JournalError, json.JSONDecodeError, UnicodeDecodeError) as error:
            response = {
                "request_id": request_id,
                "ok": False,
                "error": str(error),
            }
        self.wfile.write(_encode(response))


class JournalServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    request_queue_size = 128

    def __init__(self, socket_path: str | Path, journal: Journal) -> None:
        self.socket_path = Path(socket_path)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.journal = journal
        super().__init__(str(self.socket_path), _Handler)

    def server_close(self) -> None:
        super().server_close()
        self.socket_path.unlink(missing_ok=True)


def serve_in_thread(
    database: str | Path, socket_path: str | Path
) -> tuple[JournalServer, threading.Thread]:
    server = JournalServer(socket_path, Journal(database))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class JournalClient:
    """Client with no lifecycle methods beyond the two public tools."""

    def __init__(self, socket_path: str | Path, timeout: float = 10.0) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def _call(self, operation: str, arguments: dict[str, Any]) -> Any:
        request_id = secrets.token_hex(16)
        request = {
            "request_id": request_id,
            "operation": operation,
            "arguments": arguments,
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.socket_path)
            connection.sendall(_encode(request))
            raw = connection.makefile("rb").readline()
        response = json.loads(raw)
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            raise JournalError("journal returned an invalid response")
        if not response.get("ok"):
            raise JournalError(str(response.get("error", "journal rejected request")))
        return response.get("result")

    def add(self, run_id: str, worker_id: str, text: str) -> dict[str, Any]:
        return self._call(
            "journal_add",
            {"run_id": run_id, "worker_id": worker_id, "text": text},
        )

    def search(self, run_id: str, query: str) -> list[dict[str, Any]]:
        return self._call("journal_search", {"run_id": run_id, "query": query})
