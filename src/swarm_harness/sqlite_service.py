"""Disposable SQLite journal service exposed over a local Unix socket."""

from __future__ import annotations

import json
import secrets
import socketserver
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .protocol import (
    JournalError,
    ProtocolError,
    Request,
    decode,
    encode,
    failure,
    ok,
    require_number,
    require_string,
)

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY, workload TEXT NOT NULL, target_repo TEXT NOT NULL,
  integration_branch TEXT NOT NULL, integration_worktree TEXT NOT NULL,
  state TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
  run_id TEXT NOT NULL, task_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
  title TEXT NOT NULL, prompt TEXT NOT NULL, checks_json TEXT NOT NULL,
  state TEXT NOT NULL, branch TEXT, worktree_path TEXT, accepted_commit TEXT,
  last_error TEXT, PRIMARY KEY (run_id, task_id),
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS dependencies (
  run_id TEXT NOT NULL, task_id TEXT NOT NULL, dependency_id TEXT NOT NULL,
  PRIMARY KEY (run_id, task_id, dependency_id),
  FOREIGN KEY (run_id, task_id) REFERENCES tasks(run_id, task_id)
);
CREATE TABLE IF NOT EXISTS claims (
  claim_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  task_id TEXT NOT NULL, worker_id TEXT NOT NULL, token TEXT NOT NULL UNIQUE,
  claimed_at REAL NOT NULL, ownership_mode TEXT NOT NULL,
  expires_at REAL, state TEXT NOT NULL, submission_json TEXT
);
CREATE TABLE IF NOT EXISTS followups (
  followup_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  task_id TEXT NOT NULL, proposal_json TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  sequence INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  event_type TEXT NOT NULL, task_id TEXT, payload_json TEXT NOT NULL,
  created_at REAL NOT NULL
);
"""


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class SQLiteJournal:
    def __init__(self, path: str | Path, clock=time.time) -> None:
        self.path = Path(path)
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate_claims(connection)

    @staticmethod
    def _migrate_claims(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"]): row for row in connection.execute("PRAGMA table_info(claims)")
        }
        if "ownership_mode" in columns and not int(columns["expires_at"]["notnull"]):
            return
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("ALTER TABLE claims RENAME TO claims_fixed_lease")
        connection.execute(
            """
            CREATE TABLE claims (
              claim_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              task_id TEXT NOT NULL, worker_id TEXT NOT NULL,
              token TEXT NOT NULL UNIQUE, claimed_at REAL NOT NULL,
              ownership_mode TEXT NOT NULL, expires_at REAL,
              state TEXT NOT NULL, submission_json TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO claims(
              claim_id,run_id,task_id,worker_id,token,claimed_at,
              ownership_mode,expires_at,state,submission_json
            )
            SELECT claim_id,run_id,task_id,worker_id,token,claimed_at,
              'observable',NULL,state,submission_json
            FROM claims_fixed_lease
            """
        )
        connection.execute("DROP TABLE claims_fixed_lease")
        connection.execute("COMMIT")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _event(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        event_type: str,
        task_id: str | None = None,
        **payload: Any,
    ) -> None:
        connection.execute(
            "INSERT INTO events(run_id,event_type,task_id,payload_json,created_at) VALUES(?,?,?,?,?)",
            (run_id, event_type, task_id, _json(payload), self.clock()),
        )

    @staticmethod
    def _run_state(connection: sqlite3.Connection, run_id: str) -> str:
        row = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise JournalError("missing_run", "run does not exist")
        return str(row[0])

    def dispatch(self, operation: str, arguments: dict[str, Any]) -> Any:
        method = getattr(self, f"op_{operation}", None)
        if method is None or not callable(method):
            raise ProtocolError(f"unknown operation: {operation}")
        return method(arguments)

    def op_create_run(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        workload = require_string(values, "workload")
        target_repo = require_string(values, "target_repo")
        integration_branch = require_string(values, "integration_branch")
        integration_worktree = require_string(values, "integration_worktree")
        tasks = values.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ProtocolError("tasks must be a nonempty list")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing:
                return {"created": False, "state": existing["state"]}
            connection.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                (
                    run_id,
                    workload,
                    target_repo,
                    integration_branch,
                    integration_worktree,
                    "running",
                    self.clock(),
                ),
            )
            identifiers: set[str] = set()
            for ordinal, task in enumerate(tasks):
                if not isinstance(task, dict):
                    raise ProtocolError("task must be an object")
                task_id = require_string(task, "id")
                if task_id in identifiers:
                    raise ProtocolError(f"duplicate task: {task_id}")
                identifiers.add(task_id)
                checks = task.get("checks", [])
                if not isinstance(checks, list) or not all(isinstance(x, str) for x in checks):
                    raise ProtocolError("task checks must be strings")
                connection.execute(
                    "INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id,
                        task_id,
                        ordinal,
                        require_string(task, "title"),
                        require_string(task, "prompt"),
                        _json(checks),
                        "pending",
                        None,
                        None,
                        None,
                        None,
                    ),
                )
            for task in tasks:
                dependencies = task.get("depends_on", [])
                if not isinstance(dependencies, list):
                    raise ProtocolError("depends_on must be a list")
                for dependency in dependencies:
                    if dependency not in identifiers:
                        raise ProtocolError(f"unknown dependency: {dependency}")
                    connection.execute(
                        "INSERT INTO dependencies VALUES(?,?,?)",
                        (run_id, task["id"], dependency),
                    )
            self._event(connection, run_id, "run_created", task_count=len(tasks))
        return {"created": True, "state": "running"}

    def _expire(self, connection: sqlite3.Connection, run_id: str) -> None:
        now = self.clock()
        rows = connection.execute(
            """
            SELECT claim_id,task_id FROM claims
            WHERE run_id=? AND state='active' AND ownership_mode='lease'
              AND expires_at IS NOT NULL AND expires_at<=?
            """,
            (run_id, now),
        ).fetchall()
        for row in rows:
            connection.execute("UPDATE claims SET state='expired' WHERE claim_id=?", (row[0],))
            connection.execute(
                "UPDATE tasks SET state='pending',worktree_path=NULL,branch=NULL WHERE run_id=? AND task_id=? AND state='claimed'",
                (run_id, row["task_id"]),
            )
            self._event(connection, run_id, "lease_expired", row["task_id"])

    def op_runnable_unprepared(self, values: dict[str, Any]) -> list[dict]:
        run_id = require_string(values, "run_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._run_state(connection, run_id) != "running":
                return []
            self._expire(connection, run_id)
            rows = connection.execute(
                """
                SELECT t.* FROM tasks t
                WHERE t.run_id=? AND t.state='pending' AND t.worktree_path IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM dependencies d JOIN tasks p
                      ON p.run_id=d.run_id AND p.task_id=d.dependency_id
                    WHERE d.run_id=t.run_id AND d.task_id=t.task_id AND p.state!='accepted'
                  ) ORDER BY t.ordinal
                """,
                (run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def op_prepare_task(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        task_id = require_string(values, "task_id")
        branch = require_string(values, "branch")
        worktree_path = require_string(values, "worktree_path")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE tasks SET branch=?,worktree_path=? WHERE run_id=? AND task_id=? AND state='pending' AND worktree_path IS NULL",
                (branch, worktree_path, run_id, task_id),
            ).rowcount
            if changed != 1:
                raise JournalError("task_not_preparable", "task is not an unprepared runnable task")
            self._event(connection, run_id, "task_prepared", task_id, branch=branch)
        return {"prepared": True}

    def op_claim_task(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        worker_id = require_string(values, "worker_id")
        raw_mode = values.get("ownership_mode")
        if raw_mode is None:
            return {"status": "stopped"}
        ownership_mode = require_string(values, "ownership_mode")
        if ownership_mode not in {"observable", "lease"}:
            raise ProtocolError("ownership_mode must be observable or lease")
        raw_lease = values.get("lease_seconds")
        lease_seconds = None if raw_lease is None else require_number(values, "lease_seconds")
        if ownership_mode == "lease" and lease_seconds is None:
            raise ProtocolError("lease ownership requires lease_seconds")
        if ownership_mode == "observable" and lease_seconds is not None:
            raise ProtocolError("observable ownership cannot have lease_seconds")
        if lease_seconds is not None and lease_seconds <= 0:
            raise ProtocolError("lease_seconds must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_state = self._run_state(connection, run_id)
            if run_state != "running":
                return {"status": run_state}
            self._expire(connection, run_id)
            row = connection.execute(
                """
                SELECT t.* FROM tasks t WHERE t.run_id=? AND t.state='pending'
                  AND t.worktree_path IS NOT NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM dependencies d JOIN tasks p
                      ON p.run_id=d.run_id AND p.task_id=d.dependency_id
                    WHERE d.run_id=t.run_id AND d.task_id=t.task_id AND p.state!='accepted'
                  ) ORDER BY t.ordinal LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if row is None:
                state = connection.execute("SELECT state FROM runs WHERE run_id=?", (run_id,)).fetchone()
                return {"status": "complete" if state and state[0] == "complete" else "idle"}
            token = secrets.token_urlsafe(32)
            now = self.clock()
            expires = None if lease_seconds is None else now + lease_seconds
            connection.execute(
                """
                INSERT INTO claims(
                  run_id,task_id,worker_id,token,claimed_at,
                  ownership_mode,expires_at,state
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    row["task_id"],
                    worker_id,
                    token,
                    now,
                    ownership_mode,
                    expires,
                    "active",
                ),
            )
            connection.execute(
                "UPDATE tasks SET state='claimed' WHERE run_id=? AND task_id=? AND state='pending'",
                (run_id, row["task_id"]),
            )
            self._event(
                connection,
                run_id,
                "task_claimed",
                row["task_id"],
                worker_id=worker_id,
                ownership_mode=ownership_mode,
            )
        return {
            "status": "claimed",
            "task_id": row["task_id"],
            "claim_token": token,
            "prompt": row["prompt"],
            "title": row["title"],
            "worktree_path": row["worktree_path"],
            "acceptance_checks": json.loads(row["checks_json"]),
            "ownership_mode": ownership_mode,
            "lease_expires_at": expires,
        }

    def op_reclaim_worker(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        worker_id = require_string(values, "worker_id")
        reason = require_string(values, "reason")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_state = self._run_state(connection, run_id)
            if run_state != "running":
                return {"run_state": run_state, "reclaimed": []}
            claims = connection.execute(
                """
                SELECT c.claim_id,c.task_id,t.worktree_path
                FROM claims c JOIN tasks t
                  ON t.run_id=c.run_id AND t.task_id=c.task_id
                WHERE c.run_id=? AND c.worker_id=? AND c.state='active'
                ORDER BY c.claim_id
                """,
                (run_id, worker_id),
            ).fetchall()
            reclaimed: list[dict[str, Any]] = []
            for claim in claims:
                connection.execute(
                    "UPDATE claims SET state='reclaimed' WHERE claim_id=?",
                    (claim["claim_id"],),
                )
                connection.execute(
                    """
                    UPDATE tasks SET state='pending'
                    WHERE run_id=? AND task_id=? AND state='claimed'
                    """,
                    (run_id, claim["task_id"]),
                )
                self._event(
                    connection,
                    run_id,
                    "task_reclaimed",
                    claim["task_id"],
                    worker_id=worker_id,
                    reason=reason,
                )
                reclaimed.append(
                    {
                        "task_id": claim["task_id"],
                        "worktree_path": claim["worktree_path"],
                    }
                )
        return {"run_state": "running", "reclaimed": reclaimed}

    def op_submit_result(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        task_id = require_string(values, "task_id")
        token = require_string(values, "claim_token")
        outcome = require_string(values, "outcome")
        if outcome not in {"completed", "failed"}:
            raise ProtocolError("outcome must be completed or failed")
        blockers = values.get("blockers", [])
        followups = values.get("proposed_followups", [])
        if not isinstance(blockers, list) or not isinstance(followups, list):
            raise ProtocolError("blockers and proposed_followups must be lists")
        submission = {
            "outcome": outcome,
            "summary": str(values.get("summary", "")),
            "commit_sha": str(values.get("commit_sha", "")),
            "blockers": blockers,
            "proposed_followups": followups,
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                "SELECT * FROM claims WHERE run_id=? AND task_id=? AND token=? AND state='active'",
                (run_id, task_id, token),
            ).fetchone()
            expired = (
                claim is not None
                and claim["ownership_mode"] == "lease"
                and claim["expires_at"] is not None
                and claim["expires_at"] <= self.clock()
            )
            if claim is None or expired:
                raise JournalError("stale_claim", "claim token is incorrect, stale, or expired")
            task = connection.execute(
                "SELECT state FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
            ).fetchone()
            if task is None or task["state"] != "claimed":
                raise JournalError("stale_claim", "task no longer belongs to this claim")
            connection.execute(
                "UPDATE claims SET state='submitted',submission_json=? WHERE claim_id=?",
                (_json(submission), claim["claim_id"]),
            )
            connection.execute(
                "UPDATE tasks SET state='submitted' WHERE run_id=? AND task_id=?",
                (run_id, task_id),
            )
            for proposal in followups:
                connection.execute(
                    "INSERT INTO followups(run_id,task_id,proposal_json,created_at) VALUES(?,?,?,?)",
                    (run_id, task_id, _json(proposal), self.clock()),
                )
            self._event(connection, run_id, "result_submitted", task_id, outcome=outcome)
        return {"recorded": True, "state": "submitted"}

    def op_submitted_tasks(self, values: dict[str, Any]) -> list[dict]:
        run_id = require_string(values, "run_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT t.*,c.submission_json FROM tasks t JOIN claims c
                  ON c.run_id=t.run_id AND c.task_id=t.task_id AND c.state='submitted'
                WHERE t.run_id=? AND t.state='submitted' ORDER BY t.ordinal
                """,
                (run_id,),
            ).fetchall()
        return [{**dict(row), "submission": json.loads(row["submission_json"])} for row in rows]

    def op_accept_task(self, values: dict[str, Any]) -> dict:
        return self._finish(values, accepted=True)

    def op_reject_task(self, values: dict[str, Any]) -> dict:
        return self._finish(values, accepted=False)

    def _finish(self, values: dict[str, Any], accepted: bool) -> dict:
        run_id = require_string(values, "run_id")
        task_id = require_string(values, "task_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if accepted:
                commit = require_string(values, "commit_sha")
                changed = connection.execute(
                    "UPDATE tasks SET state='accepted',accepted_commit=?,last_error=NULL WHERE run_id=? AND task_id=? AND state='submitted'",
                    (commit, run_id, task_id),
                ).rowcount
                event = "task_accepted"
            else:
                error = require_string(values, "error")
                changed = connection.execute(
                    "UPDATE tasks SET state='pending',branch=NULL,worktree_path=NULL,last_error=? WHERE run_id=? AND task_id=? AND state='submitted'",
                    (error, run_id, task_id),
                ).rowcount
                event = "task_rejected"
            if changed != 1:
                raise JournalError("invalid_transition", "task is not awaiting adjudication")
            connection.execute(
                "UPDATE claims SET state=? WHERE run_id=? AND task_id=? AND state='submitted'",
                ("accepted" if accepted else "rejected", run_id, task_id),
            )
            self._event(connection, run_id, event, task_id)
        return {"state": "accepted" if accepted else "pending"}

    def op_set_run_state(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        state = require_string(values, "state")
        if state not in {"running", "paused", "failed", "complete", "stopped"}:
            raise ProtocolError("invalid run state")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("UPDATE runs SET state=? WHERE run_id=?", (state, run_id)).rowcount != 1:
                raise JournalError("missing_run", "run does not exist")
            self._event(connection, run_id, f"run_{state}")
        return {"state": state}

    def op_pause_run(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._run_state(connection, run_id)
            if state in {"complete", "failed"}:
                raise JournalError("invalid_transition", f"cannot pause a {state} run")
            claims = connection.execute(
                "SELECT claim_id,task_id,worker_id FROM claims WHERE run_id=? AND state='active'",
                (run_id,),
            ).fetchall()
            for claim in claims:
                connection.execute(
                    "UPDATE claims SET state='paused' WHERE claim_id=?",
                    (claim["claim_id"],),
                )
                connection.execute(
                    "UPDATE tasks SET state='pending' WHERE run_id=? AND task_id=? AND state='claimed'",
                    (run_id, claim["task_id"]),
                )
                self._event(
                    connection,
                    run_id,
                    "task_paused",
                    claim["task_id"],
                    worker_id=claim["worker_id"],
                )
            if state != "paused":
                connection.execute("UPDATE runs SET state='paused' WHERE run_id=?", (run_id,))
                self._event(connection, run_id, "run_paused", claim_count=len(claims))
        return {"state": "paused", "paused_claims": len(claims)}

    def op_resume_run(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._run_state(connection, run_id)
            if state in {"complete", "failed"}:
                raise JournalError("invalid_transition", f"cannot resume a {state} run")
            if state != "running":
                connection.execute("UPDATE runs SET state='running' WHERE run_id=?", (run_id,))
                self._event(connection, run_id, "run_resumed", previous_state=state)
        return {"state": "running"}

    def op_run_status(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        with self._connect() as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None:
                raise JournalError("missing_run", "run does not exist")
            tasks = connection.execute(
                "SELECT task_id,title,state,branch,worktree_path,accepted_commit,last_error FROM tasks WHERE run_id=? ORDER BY ordinal",
                (run_id,),
            ).fetchall()
            followups = connection.execute(
                "SELECT task_id,proposal_json FROM followups WHERE run_id=? ORDER BY followup_id",
                (run_id,),
            ).fetchall()
        return {
            "run": dict(run),
            "tasks": [dict(row) for row in tasks],
            "proposed_followups": [
                {"task_id": row["task_id"], "proposal": json.loads(row["proposal_json"])}
                for row in followups
            ],
        }

    def op_events(self, values: dict[str, Any]) -> list[dict]:
        run_id = require_string(values, "run_id")
        after = int(values.get("after", 0))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM events WHERE run_id=? AND sequence>? ORDER BY sequence",
                (run_id, after),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request_id = "unknown"
        try:
            request = Request.from_value(decode(self.rfile.readline()))
            request_id = request.request_id
            result = self.server.journal.dispatch(request.operation, request.arguments)  # type: ignore[attr-defined]
            response = ok(request_id, result)
        except JournalError as error:
            response = failure(request_id, error.code, str(error))
        except (ProtocolError, ValueError) as error:
            response = failure(request_id, "invalid_request", str(error))
        except Exception as error:  # noqa: BLE001 - RPC boundary must return a closed error
            response = failure(request_id, "internal_error", str(error))
        self.wfile.write(encode(response))


class JournalServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str | Path, journal: SQLiteJournal) -> None:
        self.socket_path = Path(socket_path)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        self.journal = journal
        super().__init__(str(self.socket_path), _Handler)

    def server_close(self) -> None:
        super().server_close()
        if self.socket_path.exists():
            self.socket_path.unlink()


def serve_in_thread(database: str | Path, socket_path: str | Path) -> tuple[JournalServer, threading.Thread]:
    server = JournalServer(socket_path, SQLiteJournal(database))
    thread = threading.Thread(target=server.serve_forever, name="swarm-journal", daemon=True)
    thread.start()
    return server, thread
