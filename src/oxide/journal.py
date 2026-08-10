"""Fixed workflow-agnostic append/search journal kernel prototype."""

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


DEFAULT_MIN_EXACT = 5
DEFAULT_MAX_RESULTS = 10
DEFAULT_SEMANTIC_THRESHOLD = 0.6
_TOKEN = re.compile(r"[\w-]+", re.UNICODE)


SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
  record_id INTEGER PRIMARY KEY AUTOINCREMENT,
  namespace TEXT NOT NULL,
  author TEXT NOT NULL,
  text TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS records_namespace_order
ON records(namespace, record_id);
"""


def _compact(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class Journal:
    """Generic immutable records with exactly two operations."""

    def __init__(
        self,
        database: str | Path,
        clock=time.time,
        *,
        min_exact: int = DEFAULT_MIN_EXACT,
        max_results: int = DEFAULT_MAX_RESULTS,
        semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
    ) -> None:
        if (
            isinstance(min_exact, bool)
            or isinstance(max_results, bool)
            or not isinstance(min_exact, int)
            or not isinstance(max_results, int)
            or not 1 <= min_exact <= max_results
        ):
            raise JournalError("journal capacity requires 1 <= min_exact <= max_results")
        if not isinstance(semantic_threshold, (int, float)) or not 0 <= semantic_threshold <= 1:
            raise JournalError("semantic threshold must be between 0 and 1")
        self.database = Path(database)
        self.clock = clock
        self.min_exact = min_exact
        self.max_results = max_results
        self.semantic_threshold = float(semantic_threshold)
        self._cache_lock = threading.RLock()
        self._records_by_namespace: dict[str, list[dict[str, Any]]] = {}
        self._cache_highwater: dict[str, int] = {}
        self._terms_by_record: dict[int, frozenset[str]] = {}
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            existing = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if row[0] != "sqlite_sequence"
            }
            if existing and existing != {"records"}:
                raise JournalError("legacy journal must be archived before this kernel starts")
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
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
                self._require(arguments.get("namespace"), "namespace"),
                self._require(arguments.get("author"), "author"),
                self._require(arguments.get("text"), "text"),
            )
        if operation == "journal_search":
            return self.search(
                self._require(arguments.get("namespace"), "namespace"),
                self._require(arguments.get("query"), "query"),
            )
        raise JournalError("the journal exposes only journal_add and journal_search")

    def add(self, namespace: str, author: str, text: str) -> dict[str, Any]:
        if len(namespace.encode("utf-8")) > 1024 or len(author.encode("utf-8")) > 1024:
            raise JournalError("namespace and author must not exceed 1024 UTF-8 bytes")
        if not text.strip() or len(text.encode("utf-8")) > 524_288:
            raise JournalError("text must be 1..524288 UTF-8 bytes")
        created_at = self.clock()
        with self._cache_lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO records(namespace,author,text,created_at) VALUES(?,?,?,?)",
                (namespace, author, text, created_at),
            )
            sequence = int(cursor.lastrowid)
        return {"saved": True, "record_id": sequence}

    @staticmethod
    def _semantic_score(query_terms: set[str], text_terms: frozenset[str]) -> float:
        if not query_terms:
            return 0.0
        return len(query_terms & text_terms) / len(query_terms)

    @staticmethod
    def _public_record(row: sqlite3.Row | dict[str, Any], *, exact: bool) -> dict[str, Any]:
        value = dict(row)
        sequence = int(value["record_id"])
        value.update(
            stable_id=f"record:{sequence}",
            journal_sequence=sequence,
            match_kind="exact" if exact else "semantic",
        )
        return value

    def search(self, namespace: str, query: str) -> list[dict[str, Any]]:
        if len(namespace.encode("utf-8")) > 1024:
            raise JournalError("namespace must not exceed 1024 UTF-8 bytes")
        if not query or len(query.encode("utf-8")) > 4096:
            raise JournalError("query must be 1..4096 UTF-8 bytes")
        query_terms = {item.casefold() for item in _TOKEN.findall(query)}
        with self._cache_lock:
            highwater = self._cache_highwater.get(namespace, 0)
            with self._connect() as connection:
                new_rows = connection.execute(
                    """
                    SELECT record_id,namespace,author,text,created_at FROM records
                    WHERE namespace=? AND record_id>? ORDER BY record_id
                    """,
                    (namespace, highwater),
                ).fetchall()
            rows = self._records_by_namespace.setdefault(namespace, [])
            for row in new_rows:
                value = dict(row)
                sequence = int(value["record_id"])
                rows.append(value)
                self._terms_by_record[sequence] = frozenset(
                    item.casefold() for item in _TOKEN.findall(str(value["text"]))
                )
                self._cache_highwater[namespace] = sequence

            qualifying: list[tuple[dict[str, Any], bool]] = []
            for row in rows:
                text = str(row["text"])
                exact = query in text
                if (
                    exact
                    or self._semantic_score(
                        query_terms, self._terms_by_record[int(row["record_id"])]
                    )
                    >= self.semantic_threshold
                ):
                    qualifying.append((row, exact))

        exact = [item for item in qualifying if item[1]]
        required_exact = min(self.min_exact, len(exact), self.max_results)
        reserved_ids = {int(row["record_id"]) for row, _ in exact[-required_exact:]}
        selected = [item for item in qualifying if int(item[0]["record_id"]) in reserved_ids]
        remaining = [item for item in qualifying if int(item[0]["record_id"]) not in reserved_ids]
        open_slots = self.max_results - len(selected)
        if open_slots:
            selected.extend(remaining[-open_slots:])
        selected.sort(key=lambda item: int(item[0]["record_id"]))
        return [self._public_record(row, exact=is_exact) for row, is_exact in selected]


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
            response = {"request_id": request_id, "ok": False, "error": str(error)}
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
    database: str | Path,
    socket_path: str | Path,
    *,
    min_exact: int = DEFAULT_MIN_EXACT,
    max_results: int = DEFAULT_MAX_RESULTS,
    semantic_threshold: float = DEFAULT_SEMANTIC_THRESHOLD,
) -> tuple[JournalServer, threading.Thread]:
    server = JournalServer(
        socket_path,
        Journal(
            database,
            min_exact=min_exact,
            max_results=max_results,
            semantic_threshold=semantic_threshold,
        ),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class JournalClient:
    """Transport client for the immutable generic kernel operations."""

    def __init__(self, socket_path: str | Path, timeout: float = 10.0) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def _call(self, operation: str, arguments: dict[str, Any]) -> Any:
        request_id = secrets.token_hex(16)
        request = {"request_id": request_id, "operation": operation, "arguments": arguments}
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

    def add(self, namespace: str, author: str, text: str) -> dict[str, Any]:
        return self._call("journal_add", {"namespace": namespace, "author": author, "text": text})

    def search(self, namespace: str, query: str) -> list[dict[str, Any]]:
        return self._call("journal_search", {"namespace": namespace, "query": query})
