"""Disposable SQLite journal service exposed over a local Unix socket."""

from __future__ import annotations

import json
import re
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
  state TEXT NOT NULL, created_at REAL NOT NULL,
  stage_gate_json TEXT NOT NULL DEFAULT '[]'
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
CREATE TABLE IF NOT EXISTS proposals (
  proposal_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  kind TEXT NOT NULL, task_id TEXT, author_worker_id TEXT,
  payload_json TEXT NOT NULL, required_votes INTEGER NOT NULL,
  max_validators INTEGER NOT NULL, state TEXT NOT NULL, decision TEXT,
  created_at REAL NOT NULL, committed_at REAL, applied_at REAL,
  apply_result_json TEXT,
  FOREIGN KEY (run_id) REFERENCES runs(run_id)
);
CREATE TABLE IF NOT EXISTS proposal_votes (
  proposal_id INTEGER NOT NULL, worker_id TEXT NOT NULL, vote TEXT NOT NULL,
  evidence_json TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY (proposal_id, worker_id),
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
);
CREATE TABLE IF NOT EXISTS validation_claims (
  validation_claim_id INTEGER PRIMARY KEY AUTOINCREMENT,
  proposal_id INTEGER NOT NULL, run_id TEXT NOT NULL, worker_id TEXT NOT NULL,
  token TEXT NOT NULL UNIQUE, claimed_at REAL NOT NULL,
  ownership_mode TEXT NOT NULL, expires_at REAL, state TEXT NOT NULL,
  FOREIGN KEY (proposal_id) REFERENCES proposals(proposal_id)
);
CREATE INDEX IF NOT EXISTS proposals_run_state
  ON proposals(run_id,state,proposal_id);
CREATE INDEX IF NOT EXISTS validation_claims_owner
  ON validation_claims(run_id,worker_id,state);
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
            self._migrate_runs(connection)
            self._migrate_claims(connection)
            self._migrate_candidate_proposals(connection)

    @staticmethod
    def _migrate_runs(connection: sqlite3.Connection) -> None:
        columns = {
            str(row["name"]): row for row in connection.execute("PRAGMA table_info(runs)")
        }
        if "stage_gate_json" not in columns:
            connection.execute(
                "ALTER TABLE runs ADD COLUMN stage_gate_json TEXT NOT NULL DEFAULT '[]'"
            )

    def _migrate_candidate_proposals(self, connection: sqlite3.Connection) -> None:
        """Treat every exact worker commit as a candidate; quorum decides its quality."""

        rows = connection.execute(
            "SELECT proposal_id,run_id,task_id,payload_json FROM proposals WHERE kind='task_retry' AND state='open'"
        ).fetchall()
        for row in rows:
            payload = json.loads(row["payload_json"])
            commit = str(payload.get("candidate_commit", ""))
            if not re.fullmatch(r"[0-9a-f]{40}", commit):
                continue
            payload["proposed_action"] = "accept"
            connection.execute(
                "UPDATE proposals SET kind='task_acceptance',payload_json=? WHERE proposal_id=?",
                (_json(payload), row["proposal_id"]),
            )
            self._event(
                connection,
                str(row["run_id"]),
                "candidate_proposal_normalized",
                row["task_id"],
                proposal_id=row["proposal_id"],
            )

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

    def _open_proposal(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        kind: str,
        task_id: str | None,
        author_worker_id: str | None,
        payload: dict[str, Any],
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO proposals(
              run_id,kind,task_id,author_worker_id,payload_json,
              required_votes,max_validators,state,created_at
            ) VALUES(?,?,?,?,?,?,?,'open',?)
            """,
            (
                run_id,
                kind,
                task_id,
                author_worker_id,
                _json(payload),
                2,
                3,
                self.clock(),
            ),
        )
        proposal_id = int(cursor.lastrowid)
        self._event(
            connection,
            run_id,
            "proposal_opened",
            task_id,
            proposal_id=proposal_id,
            kind=kind,
            author_worker_id=author_worker_id,
            quorum="2-of-3",
        )
        return proposal_id

    @staticmethod
    def _claim_configuration(values: dict[str, Any]) -> tuple[str, float | None]:
        raw_mode = values.get("ownership_mode")
        if raw_mode is None:
            raise ProtocolError("ownership_mode is required")
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
        return ownership_mode, lease_seconds

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
        stage_gate = values.get("stage_gate", [])
        if not isinstance(stage_gate, list) or not all(
            isinstance(command, str) for command in stage_gate
        ):
            raise ProtocolError("stage_gate must be a list of strings")
        tasks = values.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise ProtocolError("tasks must be a nonempty list")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing:
                stored_gate = json.loads(existing["stage_gate_json"])
                if not stored_gate and stage_gate:
                    connection.execute(
                        "UPDATE runs SET stage_gate_json=? WHERE run_id=?",
                        (_json(stage_gate), run_id),
                    )
                    self._event(
                        connection,
                        run_id,
                        "stage_gate_bound",
                        command_count=len(stage_gate),
                    )
                elif stage_gate and stored_gate != stage_gate:
                    raise JournalError(
                        "run_configuration_mismatch",
                        "stage gate differs from the journaled run configuration",
                    )
                return {"created": False, "state": existing["state"]}
            connection.execute(
                """
                INSERT INTO runs(
                  run_id,workload,target_repo,integration_branch,
                  integration_worktree,state,created_at,stage_gate_json
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    workload,
                    target_repo,
                    integration_branch,
                    integration_worktree,
                    "running",
                    self.clock(),
                    _json(stage_gate),
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
        validations = connection.execute(
            """
            SELECT validation_claim_id,proposal_id FROM validation_claims
            WHERE run_id=? AND state='active' AND ownership_mode='lease'
              AND expires_at IS NOT NULL AND expires_at<=?
            """,
            (run_id, now),
        ).fetchall()
        for validation in validations:
            connection.execute(
                "UPDATE validation_claims SET state='expired' WHERE validation_claim_id=?",
                (validation["validation_claim_id"],),
            )
            self._event(
                connection,
                run_id,
                "validation_expired",
                proposal_id=validation["proposal_id"],
            )

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
        if values.get("ownership_mode") is None:
            return {"status": "stopped"}
        ownership_mode, lease_seconds = self._claim_configuration(values)
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
            "work_kind": "implementation",
            "task_id": row["task_id"],
            "claim_token": token,
            "prompt": row["prompt"],
            "title": row["title"],
            "worktree_path": row["worktree_path"],
            "acceptance_checks": json.loads(row["checks_json"]),
            "ownership_mode": ownership_mode,
            "lease_expires_at": expires,
        }

    def op_claim_work(self, values: dict[str, Any]) -> dict:
        """Atomically prefer independent validation, then routine implementation."""

        run_id = require_string(values, "run_id")
        worker_id = require_string(values, "worker_id")
        ownership_mode, lease_seconds = self._claim_configuration(values)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run_state = self._run_state(connection, run_id)
            if run_state != "running":
                return {"status": run_state}
            self._expire(connection, run_id)
            proposal = connection.execute(
                """
                SELECT p.*,t.title,t.prompt,t.checks_json,t.worktree_path,t.branch,
                       r.integration_worktree
                FROM proposals p
                JOIN runs r ON r.run_id=p.run_id
                LEFT JOIN tasks t
                  ON t.run_id=p.run_id AND t.task_id=p.task_id
                WHERE p.run_id=? AND p.state='open'
                  AND (p.author_worker_id IS NULL OR p.author_worker_id!=?)
                  AND NOT EXISTS (
                    SELECT 1 FROM proposal_votes v
                    WHERE v.proposal_id=p.proposal_id AND v.worker_id=?
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM validation_claims own
                    WHERE own.proposal_id=p.proposal_id AND own.worker_id=?
                      AND own.state='active'
                  )
                  AND (
                    (SELECT COUNT(*) FROM proposal_votes votes
                     WHERE votes.proposal_id=p.proposal_id) +
                    (SELECT COUNT(*) FROM validation_claims active
                     WHERE active.proposal_id=p.proposal_id AND active.state='active')
                  ) < p.max_validators
                ORDER BY p.proposal_id
                LIMIT 1
                """,
                (run_id, worker_id, worker_id, worker_id),
            ).fetchone()
            if proposal is None:
                task = connection.execute(
                    """
                    SELECT t.* FROM tasks t WHERE t.run_id=? AND t.state='pending'
                      AND t.worktree_path IS NOT NULL
                      AND NOT EXISTS (
                        SELECT 1 FROM dependencies d JOIN tasks p
                          ON p.run_id=d.run_id AND p.task_id=d.dependency_id
                        WHERE d.run_id=t.run_id AND d.task_id=t.task_id
                          AND p.state!='accepted'
                      ) ORDER BY t.ordinal LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if task is None:
                    return {"status": "idle"}
                token = secrets.token_urlsafe(32)
                now = self.clock()
                expires = None if lease_seconds is None else now + lease_seconds
                connection.execute(
                    """
                    INSERT INTO claims(
                      run_id,task_id,worker_id,token,claimed_at,
                      ownership_mode,expires_at,state
                    ) VALUES(?,?,?,?,?,?,?,'active')
                    """,
                    (
                        run_id,
                        task["task_id"],
                        worker_id,
                        token,
                        now,
                        ownership_mode,
                        expires,
                    ),
                )
                connection.execute(
                    "UPDATE tasks SET state='claimed' WHERE run_id=? AND task_id=? AND state='pending'",
                    (run_id, task["task_id"]),
                )
                self._event(
                    connection,
                    run_id,
                    "task_claimed",
                    task["task_id"],
                    worker_id=worker_id,
                    ownership_mode=ownership_mode,
                )
                return {
                    "status": "claimed",
                    "work_kind": "implementation",
                    "task_id": task["task_id"],
                    "claim_token": token,
                    "prompt": task["prompt"],
                    "title": task["title"],
                    "worktree_path": task["worktree_path"],
                    "acceptance_checks": json.loads(task["checks_json"]),
                    "ownership_mode": ownership_mode,
                    "lease_expires_at": expires,
                }
            token = secrets.token_urlsafe(32)
            now = self.clock()
            expires = None if lease_seconds is None else now + lease_seconds
            connection.execute(
                """
                INSERT INTO validation_claims(
                  proposal_id,run_id,worker_id,token,claimed_at,
                  ownership_mode,expires_at,state
                ) VALUES(?,?,?,?,?,?,?,'active')
                """,
                (
                    proposal["proposal_id"],
                    run_id,
                    worker_id,
                    token,
                    now,
                    ownership_mode,
                    expires,
                ),
            )
            self._event(
                connection,
                run_id,
                "validation_claimed",
                proposal["task_id"],
                proposal_id=proposal["proposal_id"],
                worker_id=worker_id,
                ownership_mode=ownership_mode,
            )
            payload = json.loads(proposal["payload_json"])
            return {
                "status": "claimed",
                "work_kind": "validation",
                "proposal_id": proposal["proposal_id"],
                "proposal_kind": proposal["kind"],
                "claim_token": token,
                "task_id": proposal["task_id"],
                "title": proposal["title"] or proposal["kind"],
                "prompt": proposal["prompt"] or "Validate the journal proposal.",
                "worktree_path": proposal["worktree_path"]
                or proposal["integration_worktree"],
                "acceptance_checks": payload.get(
                    "checks",
                    json.loads(proposal["checks_json"])
                    if proposal["checks_json"] is not None
                    else [],
                ),
                "payload": payload,
                "author_worker_id": proposal["author_worker_id"],
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
            validations = connection.execute(
                """
                SELECT validation_claim_id,proposal_id
                FROM validation_claims
                WHERE run_id=? AND worker_id=? AND state='active'
                ORDER BY validation_claim_id
                """,
                (run_id, worker_id),
            ).fetchall()
            reclaimed_validations: list[int] = []
            for validation in validations:
                connection.execute(
                    "UPDATE validation_claims SET state='reclaimed' WHERE validation_claim_id=?",
                    (validation["validation_claim_id"],),
                )
                self._event(
                    connection,
                    run_id,
                    "validation_reclaimed",
                    proposal_id=validation["proposal_id"],
                    worker_id=worker_id,
                    reason=reason,
                )
                reclaimed_validations.append(int(validation["proposal_id"]))
        result: dict[str, Any] = {"run_state": "running", "reclaimed": reclaimed}
        if reclaimed_validations:
            result["reclaimed_validations"] = reclaimed_validations
        return result

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
                "SELECT * FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
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
            has_candidate = re.fullmatch(r"[0-9a-f]{40}", submission["commit_sha"]) is not None
            proposal_kind = "task_acceptance" if has_candidate else "task_retry"
            proposal_id = self._open_proposal(
                connection,
                run_id=run_id,
                kind=proposal_kind,
                task_id=task_id,
                author_worker_id=str(claim["worker_id"]),
                payload={
                    "proposed_action": "accept" if has_candidate else "retry",
                    "submission": submission,
                    "candidate_commit": submission["commit_sha"],
                    "checks": json.loads(task["checks_json"]),
                    "worktree_path": task["worktree_path"],
                    "branch": task["branch"],
                },
            )
            self._event(
                connection,
                run_id,
                "result_submitted",
                task_id,
                outcome=outcome,
                proposal_id=proposal_id,
            )
        return {
            "recorded": True,
            "state": "submitted",
            "proposal_id": proposal_id,
        }

    def op_propose_change(self, values: dict[str, Any]) -> dict:
        """Open a permissionless proposal; opening it grants no transition authority."""

        run_id = require_string(values, "run_id")
        worker_id = require_string(values, "worker_id")
        kind = require_string(values, "kind")
        if kind not in {"task_decomposition", "dependency_change", "retry_task"}:
            raise ProtocolError("unsupported proposal kind")
        payload = values.get("payload")
        if not isinstance(payload, dict):
            raise ProtocolError("proposal payload must be an object")
        task_id = values.get("task_id")
        if task_id is not None and not isinstance(task_id, str):
            raise ProtocolError("task_id must be a string when present")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._run_state(connection, run_id) != "running":
                raise JournalError("run_not_running", "proposals require a running run")
            proposal_id = self._open_proposal(
                connection,
                run_id=run_id,
                kind=kind,
                task_id=task_id,
                author_worker_id=worker_id,
                payload=payload,
            )
        return {"proposal_id": proposal_id, "state": "open"}

    def _queue_retry(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        task_id: str,
        reason: str,
    ) -> None:
        changed = connection.execute(
            """
            UPDATE tasks
            SET state='pending',branch=NULL,worktree_path=NULL,
                accepted_commit=NULL,last_error=?
            WHERE run_id=? AND task_id=?
              AND state IN ('submitted','integrating','accepted')
            """,
            (reason, run_id, task_id),
        ).rowcount
        if changed != 1:
            raise JournalError("invalid_transition", "task cannot be retried in its state")
        connection.execute(
            """
            UPDATE claims SET state='rejected'
            WHERE run_id=? AND task_id=? AND state='submitted'
            """,
            (run_id, task_id),
        )
        self._event(connection, run_id, "task_retry_committed", task_id, reason=reason)

    def _apply_task_decomposition(
        self,
        connection: sqlite3.Connection,
        proposal: sqlite3.Row,
        payload: dict[str, Any],
    ) -> None:
        tasks = payload.get("tasks")
        if not isinstance(tasks, list) or not tasks:
            raise JournalError("invalid_proposal", "task decomposition requires tasks")
        run_id = str(proposal["run_id"])
        next_ordinal = int(
            connection.execute(
                "SELECT COALESCE(MAX(ordinal),-1)+1 FROM tasks WHERE run_id=?", (run_id,)
            ).fetchone()[0]
        )
        known = {
            str(row[0])
            for row in connection.execute(
                "SELECT task_id FROM tasks WHERE run_id=?", (run_id,)
            )
        }
        proposed: list[dict[str, Any]] = []
        for raw in tasks:
            if not isinstance(raw, dict):
                raise JournalError("invalid_proposal", "decomposed task must be an object")
            task_id = str(raw.get("id", ""))
            checks = raw.get("checks", [])
            dependencies = raw.get("depends_on", [])
            if (
                not task_id
                or task_id in known
                or not isinstance(checks, list)
                or not all(isinstance(item, str) for item in checks)
                or not isinstance(dependencies, list)
                or not all(isinstance(item, str) for item in dependencies)
            ):
                raise JournalError("invalid_proposal", "decomposed task schema is invalid")
            known.add(task_id)
            proposed.append({**raw, "id": task_id})
        for offset, task in enumerate(proposed):
            connection.execute(
                """
                INSERT INTO tasks(
                  run_id,task_id,ordinal,title,prompt,checks_json,state,
                  branch,worktree_path,accepted_commit,last_error
                ) VALUES(?,?,?,?,?,?,'pending',NULL,NULL,NULL,NULL)
                """,
                (
                    run_id,
                    task["id"],
                    next_ordinal + offset,
                    str(task.get("title", task["id"])),
                    str(task.get("prompt", "")),
                    _json(task.get("checks", [])),
                ),
            )
        for task in proposed:
            for dependency in task.get("depends_on", []):
                if dependency not in known or dependency == task["id"]:
                    raise JournalError("invalid_proposal", "dependency is unknown or cyclic")
                connection.execute(
                    "INSERT INTO dependencies VALUES(?,?,?)",
                    (run_id, task["id"], dependency),
                )

    def _apply_dependency_change(
        self,
        connection: sqlite3.Connection,
        proposal: sqlite3.Row,
        payload: dict[str, Any],
    ) -> None:
        run_id = str(proposal["run_id"])
        task_id = str(payload.get("task_id") or proposal["task_id"] or "")
        dependencies = payload.get("depends_on")
        if not task_id or not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            raise JournalError("invalid_proposal", "dependency change schema is invalid")
        task = connection.execute(
            "SELECT state FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id)
        ).fetchone()
        if task is None or task["state"] != "pending":
            raise JournalError("invalid_transition", "only a pending task may change dependencies")
        known = {
            str(row[0])
            for row in connection.execute(
                "SELECT task_id FROM tasks WHERE run_id=?", (run_id,)
            )
        }
        if task_id in dependencies or any(item not in known for item in dependencies):
            raise JournalError("invalid_proposal", "dependency is unknown or self-referential")
        connection.execute(
            "DELETE FROM dependencies WHERE run_id=? AND task_id=?", (run_id, task_id)
        )
        for dependency in dependencies:
            connection.execute(
                "INSERT INTO dependencies VALUES(?,?,?)",
                (run_id, task_id, dependency),
            )

    def _commit_approved_proposal(
        self,
        connection: sqlite3.Connection,
        proposal: sqlite3.Row,
    ) -> str:
        proposal_id = int(proposal["proposal_id"])
        run_id = str(proposal["run_id"])
        kind = str(proposal["kind"])
        task_id = proposal["task_id"]
        payload = json.loads(proposal["payload_json"])
        now = self.clock()
        if kind == "task_acceptance":
            changed = connection.execute(
                "UPDATE tasks SET state='integrating' WHERE run_id=? AND task_id=? AND state='submitted'",
                (run_id, task_id),
            ).rowcount
            if changed != 1:
                raise JournalError("invalid_transition", "candidate is no longer awaiting validation")
            connection.execute(
                """
                UPDATE proposals SET state='committed',decision='approve',committed_at=?
                WHERE proposal_id=? AND state='open'
                """,
                (now, proposal_id),
            )
            state = "committed"
        elif kind in {"task_retry", "retry_task"}:
            retry_id = str(task_id or payload.get("task_id") or "")
            self._queue_retry(
                connection,
                run_id,
                retry_id,
                str(payload.get("reason") or "retry approved by validation quorum"),
            )
            connection.execute(
                """
                UPDATE proposals SET state='applied',decision='approve',
                  committed_at=?,applied_at=?,apply_result_json=?
                WHERE proposal_id=? AND state='open'
                """,
                (now, now, _json({"task_state": "pending"}), proposal_id),
            )
            state = "applied"
        elif kind == "task_decomposition":
            self._apply_task_decomposition(connection, proposal, payload)
            connection.execute(
                """
                UPDATE proposals SET state='applied',decision='approve',
                  committed_at=?,applied_at=?,apply_result_json=?
                WHERE proposal_id=? AND state='open'
                """,
                (now, now, _json({"task_count": len(payload["tasks"])}), proposal_id),
            )
            state = "applied"
        elif kind == "dependency_change":
            self._apply_dependency_change(connection, proposal, payload)
            connection.execute(
                """
                UPDATE proposals SET state='applied',decision='approve',
                  committed_at=?,applied_at=?,apply_result_json=?
                WHERE proposal_id=? AND state='open'
                """,
                (now, now, _json({"dependencies_changed": True}), proposal_id),
            )
            state = "applied"
        elif kind == "stage_completion":
            states = connection.execute(
                "SELECT state FROM tasks WHERE run_id=?", (run_id,)
            ).fetchall()
            if not states or any(row["state"] != "accepted" for row in states):
                raise JournalError("invalid_transition", "stage tasks are no longer all accepted")
            connection.execute("UPDATE runs SET state='complete' WHERE run_id=?", (run_id,))
            connection.execute(
                """
                UPDATE proposals SET state='applied',decision='approve',
                  committed_at=?,applied_at=?,apply_result_json=?
                WHERE proposal_id=? AND state='open'
                """,
                (now, now, _json({"run_state": "complete"}), proposal_id),
            )
            self._event(connection, run_id, "run_complete", proposal_id=proposal_id)
            state = "applied"
        else:
            raise JournalError("invalid_proposal", f"unsupported proposal kind: {kind}")
        self._event(
            connection,
            run_id,
            "proposal_quorum_committed",
            task_id,
            proposal_id=proposal_id,
            decision="approve",
            state=state,
        )
        return state

    def _commit_rejected_proposal(
        self,
        connection: sqlite3.Connection,
        proposal: sqlite3.Row,
    ) -> str:
        proposal_id = int(proposal["proposal_id"])
        run_id = str(proposal["run_id"])
        task_id = proposal["task_id"]
        now = self.clock()
        state = "rejected"
        if proposal["kind"] == "task_acceptance" and task_id:
            self._queue_retry(
                connection,
                run_id,
                str(task_id),
                "candidate rejected by independent validation quorum",
            )
            state = "applied"
        connection.execute(
            """
            UPDATE proposals SET state=?,decision='reject',committed_at=?,applied_at=?
            WHERE proposal_id=? AND state='open'
            """,
            (state, now, now if state == "applied" else None, proposal_id),
        )
        self._event(
            connection,
            run_id,
            "proposal_quorum_committed",
            task_id,
            proposal_id=proposal_id,
            decision="reject",
            state=state,
        )
        return state

    def op_submit_validation(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        worker_id = require_string(values, "worker_id")
        token = require_string(values, "claim_token")
        proposal_id = int(values.get("proposal_id", 0))
        vote = require_string(values, "vote")
        if proposal_id <= 0 or vote not in {"approve", "reject"}:
            raise ProtocolError("validation requires a proposal and approve/reject vote")
        evidence = values.get("evidence")
        if not isinstance(evidence, dict):
            raise ProtocolError("validation evidence must be an object")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            claim = connection.execute(
                """
                SELECT * FROM validation_claims
                WHERE proposal_id=? AND run_id=? AND worker_id=? AND token=?
                """,
                (proposal_id, run_id, worker_id, token),
            ).fetchone()
            if claim is None:
                raise JournalError("stale_claim", "validation claim is incorrect or stale")
            proposal = connection.execute(
                "SELECT * FROM proposals WHERE proposal_id=? AND run_id=?",
                (proposal_id, run_id),
            ).fetchone()
            if claim["state"] != "active" or proposal is None or proposal["state"] != "open":
                return {"recorded": False, "state": "superseded"}
            expired = (
                claim["ownership_mode"] == "lease"
                and claim["expires_at"] is not None
                and claim["expires_at"] <= self.clock()
            )
            if expired:
                raise JournalError("stale_claim", "validation claim expired")
            if proposal["author_worker_id"] == worker_id:
                raise JournalError("author_excluded", "proposal authors cannot validate themselves")
            connection.execute(
                """
                INSERT INTO proposal_votes(proposal_id,worker_id,vote,evidence_json,created_at)
                VALUES(?,?,?,?,?)
                """,
                (proposal_id, worker_id, vote, _json(evidence), self.clock()),
            )
            connection.execute(
                "UPDATE validation_claims SET state='completed' WHERE validation_claim_id=?",
                (claim["validation_claim_id"],),
            )
            counts = {
                str(row["vote"]): int(row["count"])
                for row in connection.execute(
                    """
                    SELECT vote,COUNT(*) AS count FROM proposal_votes
                    WHERE proposal_id=? GROUP BY vote
                    """,
                    (proposal_id,),
                )
            }
            state = "open"
            decision = None
            if counts.get("approve", 0) >= int(proposal["required_votes"]):
                state = self._commit_approved_proposal(connection, proposal)
                decision = "approve"
            elif counts.get("reject", 0) >= int(proposal["required_votes"]):
                state = self._commit_rejected_proposal(connection, proposal)
                decision = "reject"
            if decision is not None:
                connection.execute(
                    """
                    UPDATE validation_claims SET state='superseded'
                    WHERE proposal_id=? AND state='active'
                    """,
                    (proposal_id,),
                )
            self._event(
                connection,
                run_id,
                "validation_recorded",
                proposal["task_id"],
                proposal_id=proposal_id,
                worker_id=worker_id,
                vote=vote,
                approvals=counts.get("approve", 0),
                rejections=counts.get("reject", 0),
            )
        return {
            "recorded": True,
            "state": state,
            "decision": decision,
            "approvals": counts.get("approve", 0),
            "rejections": counts.get("reject", 0),
        }

    def op_committed_proposals(self, values: dict[str, Any]) -> list[dict]:
        run_id = require_string(values, "run_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM proposals
                WHERE run_id=? AND state='committed' AND kind='task_acceptance'
                ORDER BY proposal_id
                """,
                (run_id,),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def op_apply_proposal(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        proposal_id = int(values.get("proposal_id", 0))
        success = values.get("success")
        if proposal_id <= 0 or not isinstance(success, bool):
            raise ProtocolError("apply requires proposal_id and boolean success")
        integration_commit = str(values.get("integration_commit", ""))
        error = str(values.get("error", ""))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            proposal = connection.execute(
                """
                SELECT * FROM proposals
                WHERE proposal_id=? AND run_id=? AND kind='task_acceptance'
                  AND state='committed' AND decision='approve'
                """,
                (proposal_id, run_id),
            ).fetchone()
            if proposal is None:
                raise JournalError("invalid_transition", "proposal is not committed for integration")
            task_id = str(proposal["task_id"])
            now = self.clock()
            if success:
                if len(integration_commit) != 40:
                    raise ProtocolError("integration_commit must be an exact Git object id")
                changed = connection.execute(
                    """
                    UPDATE tasks SET state='accepted',accepted_commit=?,last_error=NULL
                    WHERE run_id=? AND task_id=? AND state='integrating'
                    """,
                    (integration_commit, run_id, task_id),
                ).rowcount
                if changed != 1:
                    raise JournalError("invalid_transition", "task is not awaiting integration")
                connection.execute(
                    "UPDATE claims SET state='accepted' WHERE run_id=? AND task_id=? AND state='submitted'",
                    (run_id, task_id),
                )
                result = {"integration_commit": integration_commit}
                self._event(
                    connection,
                    run_id,
                    "task_accepted",
                    task_id,
                    proposal_id=proposal_id,
                    integration_commit=integration_commit,
                )
            else:
                connection.execute(
                    "UPDATE tasks SET state='submitted',last_error=? WHERE run_id=? AND task_id=? AND state='integrating'",
                    (error or "integration failed", run_id, task_id),
                )
                retry_id = self._open_proposal(
                    connection,
                    run_id=run_id,
                    kind="task_retry",
                    task_id=task_id,
                    author_worker_id="launcher",
                    payload={
                        "proposed_action": "retry",
                        "task_id": task_id,
                        "reason": error or "integration failed",
                        "source_proposal_id": proposal_id,
                    },
                )
                result = {"error": error or "integration failed", "retry_proposal_id": retry_id}
            connection.execute(
                """
                UPDATE proposals SET state='applied',applied_at=?,apply_result_json=?
                WHERE proposal_id=?
                """,
                (now, _json(result), proposal_id),
            )
        return {"state": "accepted" if success else "submitted", **result}

    def op_ensure_stage_completion(self, values: dict[str, Any]) -> dict:
        run_id = require_string(values, "run_id")
        integration_head = require_string(values, "integration_head")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._run_state(connection, run_id) != "running":
                return {"opened": False}
            states = connection.execute(
                "SELECT state FROM tasks WHERE run_id=?", (run_id,)
            ).fetchall()
            if not states or any(row["state"] != "accepted" for row in states):
                return {"opened": False}
            existing = connection.execute(
                "SELECT proposal_id,state FROM proposals WHERE run_id=? AND kind='stage_completion' ORDER BY proposal_id DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if existing is not None:
                return {
                    "opened": False,
                    "proposal_id": existing["proposal_id"],
                    "state": existing["state"],
                }
            run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            proposal_id = self._open_proposal(
                connection,
                run_id=run_id,
                kind="stage_completion",
                task_id=None,
                author_worker_id="launcher",
                payload={
                    "proposed_action": "complete",
                    "integration_head": integration_head,
                    "worktree_path": run["integration_worktree"],
                    "checks": json.loads(run["stage_gate_json"]),
                },
            )
        return {"opened": True, "proposal_id": proposal_id, "state": "open"}

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
        raise JournalError(
            "authority_removed",
            "task acceptance requires an independent validation quorum",
        )

    def op_reject_task(self, values: dict[str, Any]) -> dict:
        raise JournalError(
            "authority_removed",
            "task retry requires an independent validation quorum",
        )

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
        if state in {"complete", "failed"}:
            raise JournalError(
                "authority_removed",
                "terminal stage decisions require an independent validation quorum",
            )
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
            validation_claims = connection.execute(
                """
                SELECT validation_claim_id,proposal_id,worker_id
                FROM validation_claims WHERE run_id=? AND state='active'
                """,
                (run_id,),
            ).fetchall()
            for validation in validation_claims:
                connection.execute(
                    "UPDATE validation_claims SET state='paused' WHERE validation_claim_id=?",
                    (validation["validation_claim_id"],),
                )
                self._event(
                    connection,
                    run_id,
                    "validation_paused",
                    proposal_id=validation["proposal_id"],
                    worker_id=validation["worker_id"],
                )
            if state != "paused":
                connection.execute("UPDATE runs SET state='paused' WHERE run_id=?", (run_id,))
                self._event(
                    connection,
                    run_id,
                    "run_paused",
                    claim_count=len(claims),
                    validation_count=len(validation_claims),
                )
        result: dict[str, Any] = {"state": "paused", "paused_claims": len(claims)}
        if validation_claims:
            result["paused_validations"] = len(validation_claims)
        return result

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
            proposals = connection.execute(
                """
                SELECT p.proposal_id,p.kind,p.task_id,p.author_worker_id,p.state,
                       p.decision,p.required_votes,p.max_validators,
                       COALESCE(SUM(CASE WHEN v.vote='approve' THEN 1 ELSE 0 END),0) approvals,
                       COALESCE(SUM(CASE WHEN v.vote='reject' THEN 1 ELSE 0 END),0) rejections
                FROM proposals p LEFT JOIN proposal_votes v
                  ON v.proposal_id=p.proposal_id
                WHERE p.run_id=?
                GROUP BY p.proposal_id
                ORDER BY p.proposal_id
                """,
                (run_id,),
            ).fetchall()
        return {
            "run": dict(run),
            "tasks": [dict(row) for row in tasks],
            "proposed_followups": [
                {"task_id": row["task_id"], "proposal": json.loads(row["proposal_json"])}
                for row in followups
            ],
            "proposals": [dict(row) for row in proposals],
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
