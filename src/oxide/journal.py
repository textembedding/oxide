"""Fixed workflow-agnostic append/search journal kernel prototype."""

from __future__ import annotations

import hashlib
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
_MAX_QUERY_BYTES = 4096
_EXACT_GRAM_BYTES = 16
_EXACT_GRAM_STRIDE = 4
_EXACT_INDEX_VERSION = "aligned-bytegram-blake2b-16x4-v1"
_LEXICAL_INDEX_VERSION = "casefold-token-v1"


_INDEX_TABLES = {
    "exact_counts",
    "exact_postings",
    "index_receipts",
    "index_metadata",
    "index_namespaces",
    "lexical_counts",
    "lexical_postings",
}


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

CREATE TABLE IF NOT EXISTS index_metadata (
  singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
  exact_version TEXT NOT NULL,
  lexical_version TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_namespaces (
  index_namespace_id INTEGER PRIMARY KEY,
  namespace TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS exact_postings (
  index_namespace_id INTEGER NOT NULL REFERENCES index_namespaces(index_namespace_id),
  gram BLOB NOT NULL,
  record_id INTEGER NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
  PRIMARY KEY(index_namespace_id, gram, record_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS exact_counts (
  index_namespace_id INTEGER NOT NULL REFERENCES index_namespaces(index_namespace_id),
  gram BLOB NOT NULL,
  record_count INTEGER NOT NULL CHECK(record_count > 0),
  PRIMARY KEY(index_namespace_id, gram)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS lexical_postings (
  index_namespace_id INTEGER NOT NULL REFERENCES index_namespaces(index_namespace_id),
  term TEXT NOT NULL,
  record_id INTEGER NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
  PRIMARY KEY(index_namespace_id, term, record_id)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS lexical_counts (
  index_namespace_id INTEGER NOT NULL REFERENCES index_namespaces(index_namespace_id),
  term TEXT NOT NULL,
  record_count INTEGER NOT NULL CHECK(record_count > 0),
  PRIMARY KEY(index_namespace_id, term)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS index_receipts (
  record_id INTEGER PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
  index_namespace_id INTEGER NOT NULL REFERENCES index_namespaces(index_namespace_id),
  text_sha256 TEXT NOT NULL,
  exact_gram_count INTEGER NOT NULL,
  term_count INTEGER NOT NULL
);
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
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            existing = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
                if row[0] != "sqlite_sequence"
            }
            if existing and (
                "records" not in existing or not existing <= ({"records"} | _INDEX_TABLES)
            ):
                raise JournalError("legacy journal must be archived before this kernel starts")
            try:
                connection.executescript(SCHEMA)
                self._repair_indexes(connection)
            except sqlite3.Error as error:
                raise JournalError(f"journal index preparation failed: {error}") from error

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _terms(text: str) -> frozenset[str]:
        return frozenset(item.casefold() for item in _TOKEN.findall(text))

    @staticmethod
    def _semantic_eligible(
        query_terms: set[str], text_terms: frozenset[str], threshold: float
    ) -> bool:
        if not query_terms:
            return threshold == 0
        return len(query_terms & text_terms) / len(query_terms) >= threshold

    @staticmethod
    def _text_sha256(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def _exact_fingerprint(value: bytes) -> bytes:
        return hashlib.blake2b(value, digest_size=8).digest()

    @staticmethod
    def _exact_grams(text: str) -> frozenset[bytes]:
        value = text.encode("utf-8")
        # Sparse aligned fingerprints bound derived storage. Candidate bytes are
        # always checked against the immutable record, so collisions are inert.
        return frozenset(
            Journal._exact_fingerprint(value[offset : offset + _EXACT_GRAM_BYTES])
            for offset in range(0, len(value) - _EXACT_GRAM_BYTES + 1, _EXACT_GRAM_STRIDE)
        )

    @staticmethod
    def _index_namespace_id(
        connection: sqlite3.Connection, namespace: str, *, create: bool
    ) -> int | None:
        row = connection.execute(
            "SELECT index_namespace_id FROM index_namespaces WHERE namespace=?",
            (namespace,),
        ).fetchone()
        if row is not None:
            return int(row[0])
        if not create:
            return None
        cursor = connection.execute(
            "INSERT INTO index_namespaces(namespace) VALUES(?)", (namespace,)
        )
        return int(cursor.lastrowid)

    def _index_record(
        self,
        connection: sqlite3.Connection,
        record_id: int,
        namespace: str,
        text: str,
    ) -> None:
        index_namespace_id = self._index_namespace_id(connection, namespace, create=True)
        assert index_namespace_id is not None
        exact_grams = self._exact_grams(text)
        terms = self._terms(text)
        connection.executemany(
            """
            INSERT INTO exact_postings(index_namespace_id,gram,record_id)
            VALUES(?,?,?)
            """,
            ((index_namespace_id, gram, record_id) for gram in exact_grams),
        )
        connection.executemany(
            """
            INSERT INTO exact_counts(index_namespace_id,gram,record_count)
            VALUES(?,?,1)
            ON CONFLICT(index_namespace_id,gram)
            DO UPDATE SET record_count=record_count+1
            """,
            ((index_namespace_id, gram) for gram in exact_grams),
        )
        connection.executemany(
            """
            INSERT INTO lexical_postings(index_namespace_id,term,record_id)
            VALUES(?,?,?)
            """,
            ((index_namespace_id, term, record_id) for term in terms),
        )
        connection.executemany(
            """
            INSERT INTO lexical_counts(index_namespace_id,term,record_count)
            VALUES(?,?,1)
            ON CONFLICT(index_namespace_id,term)
            DO UPDATE SET record_count=record_count+1
            """,
            ((index_namespace_id, term) for term in terms),
        )
        connection.execute(
            """
            INSERT INTO index_receipts(
              record_id,index_namespace_id,text_sha256,exact_gram_count,term_count
            ) VALUES(?,?,?,?,?)
            """,
            (
                record_id,
                index_namespace_id,
                self._text_sha256(text),
                len(exact_grams),
                len(terms),
            ),
        )

    def _repair_indexes(self, connection: sqlite3.Connection) -> None:
        versions = connection.execute(
            "SELECT exact_version,lexical_version FROM index_metadata WHERE singleton=1"
        ).fetchone()
        record_count, record_highwater = connection.execute(
            "SELECT COUNT(*),COALESCE(MAX(record_id),0) FROM records"
        ).fetchone()
        receipt_count, receipt_highwater = connection.execute(
            "SELECT COUNT(*),COALESCE(MAX(record_id),0) FROM index_receipts"
        ).fetchone()
        expected_exact, expected_lexical = connection.execute(
            """
            SELECT COALESCE(SUM(exact_gram_count),0),COALESCE(SUM(term_count),0)
            FROM index_receipts
            """
        ).fetchone()
        actual_exact = int(connection.execute("SELECT COUNT(*) FROM exact_postings").fetchone()[0])
        actual_lexical = int(
            connection.execute("SELECT COUNT(*) FROM lexical_postings").fetchone()[0]
        )
        counted_exact = int(
            connection.execute("SELECT COALESCE(SUM(record_count),0) FROM exact_counts").fetchone()[
                0
            ]
        )
        counted_lexical = int(
            connection.execute(
                "SELECT COALESCE(SUM(record_count),0) FROM lexical_counts"
            ).fetchone()[0]
        )
        if (
            versions is not None
            and str(versions[0]) == _EXACT_INDEX_VERSION
            and str(versions[1]) == _LEXICAL_INDEX_VERSION
            and int(record_count) == int(receipt_count)
            and int(record_highwater) == int(receipt_highwater)
            and int(expected_exact) == actual_exact == counted_exact
            and int(expected_lexical) == actual_lexical == counted_lexical
        ):
            return

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DELETE FROM exact_postings")
            connection.execute("DELETE FROM exact_counts")
            connection.execute("DELETE FROM lexical_postings")
            connection.execute("DELETE FROM lexical_counts")
            connection.execute("DELETE FROM index_receipts")
            connection.execute("DELETE FROM index_namespaces")
            connection.execute("DELETE FROM index_metadata")
            rows = connection.execute(
                "SELECT record_id,namespace,text FROM records ORDER BY record_id"
            )
            for row in rows:
                self._index_record(
                    connection,
                    int(row["record_id"]),
                    str(row["namespace"]),
                    str(row["text"]),
                )
            connection.execute(
                """
                INSERT INTO index_metadata(singleton,exact_version,lexical_version)
                VALUES(1,?,?)
                """,
                (_EXACT_INDEX_VERSION, _LEXICAL_INDEX_VERSION),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                cursor = connection.execute(
                    "INSERT INTO records(namespace,author,text,created_at) VALUES(?,?,?,?)",
                    (namespace, author, text, created_at),
                )
                sequence = int(cursor.lastrowid)
                self._index_record(connection, sequence, namespace, text)
                connection.commit()
            except sqlite3.Error as error:
                connection.rollback()
                raise JournalError(f"journal index publication failed: {error}") from error
            except BaseException:
                connection.rollback()
                raise
        return {"saved": True, "record_id": sequence}

    def _exact_record_ids(
        self, connection: sqlite3.Connection, namespace: str, query: str
    ) -> list[int]:
        index_namespace_id = self._index_namespace_id(connection, namespace, create=False)
        if index_namespace_id is None:
            return []
        query_bytes = query.encode("utf-8")
        if len(query_bytes) < (_EXACT_GRAM_BYTES + _EXACT_GRAM_STRIDE - 1):
            rows = connection.execute(
                """
                SELECT record_id FROM records
                WHERE namespace=? AND instr(text,?)>0
                ORDER BY record_id DESC LIMIT ?
                """,
                (namespace, query, self.max_results),
            )
        else:
            window_count = len(query_bytes) - _EXACT_GRAM_BYTES + 1
            windows = [
                (
                    offset % _EXACT_GRAM_STRIDE,
                    self._exact_fingerprint(query_bytes[offset : offset + _EXACT_GRAM_BYTES]),
                )
                for offset in range(window_count)
            ]
            grams = {gram for _, gram in windows}
            placeholders = ",".join("?" for _ in grams)
            counts = {
                bytes(row[0]): int(row[1])
                for row in connection.execute(
                    f"""
                    SELECT gram,record_count FROM exact_counts
                    WHERE index_namespace_id=? AND gram IN ({placeholders})
                    """,
                    (index_namespace_id, *sorted(grams)),
                )
            }
            # One rare probe per alignment residue covers every possible match:
            # a long-enough occurrence must align with one of these residues.
            probes = {
                min(
                    (gram for candidate_residue, gram in windows if candidate_residue == residue),
                    key=lambda gram: (counts.get(gram, 0), gram),
                )
                for residue in range(_EXACT_GRAM_STRIDE)
            }
            probes = {gram for gram in probes if counts.get(gram, 0) > 0}
            if not probes:
                return []
            if sum(counts[gram] for gram in probes) > max(256, self.max_results * 32):
                rows = connection.execute(
                    """
                    SELECT record_id FROM records
                    WHERE namespace=? AND instr(text,?)>0
                    ORDER BY record_id DESC LIMIT ?
                    """,
                    (namespace, query, self.max_results),
                )
                return [int(row[0]) for row in rows]
            probe_placeholders = ",".join("?" for _ in probes)
            rows = connection.execute(
                f"""
                SELECT records.record_id FROM exact_postings
                CROSS JOIN records
                WHERE exact_postings.index_namespace_id=?
                  AND exact_postings.gram IN ({probe_placeholders})
                  AND records.record_id=exact_postings.record_id
                  AND records.namespace=?
                  AND instr(records.text,?)>0
                GROUP BY records.record_id
                ORDER BY records.record_id DESC LIMIT ?
                """,
                (
                    index_namespace_id,
                    *sorted(probes),
                    namespace,
                    query,
                    self.max_results,
                ),
            )
        return [int(row[0]) for row in rows]

    def _lexical_record_ids(
        self,
        connection: sqlite3.Connection,
        namespace: str,
        query_terms: set[str],
    ) -> list[int]:
        index_namespace_id = self._index_namespace_id(connection, namespace, create=False)
        if index_namespace_id is None:
            return []
        if self.semantic_threshold == 0:
            rows = connection.execute(
                """
                SELECT record_id FROM records
                WHERE namespace=? ORDER BY record_id DESC LIMIT ?
                """,
                (namespace, self.max_results),
            )
            return [int(row[0]) for row in rows]
        if not query_terms:
            return []

        required_overlap = next(
            count
            for count in range(len(query_terms) + 1)
            if count / len(query_terms) >= self.semantic_threshold
        )
        # Any set containing k of n terms intersects every (n-k+1)-term cover.
        # The rarest such cover minimizes candidates without changing eligibility.
        cover_size = len(query_terms) - required_overlap + 1
        query_terms_json = _compact(sorted(query_terms))
        counts = {
            str(row[0]): int(row[1])
            for row in connection.execute(
                """
                SELECT query_terms.value,COALESCE((
                  SELECT lexical_counts.record_count FROM lexical_counts
                  WHERE lexical_counts.index_namespace_id=?
                    AND lexical_counts.term=query_terms.value
                ),0)
                FROM json_each(?) AS query_terms
                """,
                (index_namespace_id, query_terms_json),
            )
        }
        cover = sorted(query_terms, key=lambda term: (counts.get(term, 0), term))[:cover_size]
        cover_json = _compact(cover)

        selected: list[int] = []
        before_record_id = (1 << 63) - 1
        page_size = max(64, min(self.max_results * 4, 4096))
        while len(selected) < self.max_results:
            page = [
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT DISTINCT lexical_postings.record_id
                    FROM lexical_postings
                    WHERE lexical_postings.index_namespace_id=?
                      AND lexical_postings.term IN (SELECT value FROM json_each(?))
                      AND lexical_postings.record_id<?
                    ORDER BY lexical_postings.record_id DESC LIMIT ?
                    """,
                    (index_namespace_id, cover_json, before_record_id, page_size),
                )
            ]
            if not page:
                break
            eligible = {
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT lexical_postings.record_id
                    FROM lexical_postings
                    WHERE lexical_postings.index_namespace_id=?
                      AND lexical_postings.term IN (SELECT value FROM json_each(?))
                      AND lexical_postings.record_id IN (
                        SELECT CAST(value AS INTEGER) FROM json_each(?)
                      )
                    GROUP BY lexical_postings.record_id HAVING COUNT(*)>=?
                    """,
                    (
                        index_namespace_id,
                        query_terms_json,
                        _compact(page),
                        required_overlap,
                    ),
                )
            }
            selected.extend(record_id for record_id in page if record_id in eligible)
            before_record_id = page[-1]
            if len(page) < page_size:
                break
        return selected[: self.max_results]

    @staticmethod
    def _records_for_ids(
        connection: sqlite3.Connection, namespace: str, record_ids: set[int]
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        pending = sorted(record_ids)
        for offset in range(0, len(pending), 500):
            values = pending[offset : offset + 500]
            placeholders = ",".join("?" for _ in values)
            rows = connection.execute(
                f"""
                SELECT record_id,namespace,author,text,created_at FROM records
                WHERE namespace=? AND record_id IN ({placeholders}) ORDER BY record_id
                """,
                (namespace, *values),
            )
            records.extend(dict(row) for row in rows)
        records.sort(key=lambda row: int(row["record_id"]))
        return records

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
        if not query or len(query.encode("utf-8")) > _MAX_QUERY_BYTES:
            raise JournalError("query must be 1..4096 UTF-8 bytes")
        query_terms = {item.casefold() for item in _TOKEN.findall(query)}
        with self._connect() as connection:
            exact_ids = set(self._exact_record_ids(connection, namespace, query))
            semantic_ids = set(self._lexical_record_ids(connection, namespace, query_terms))
            rows = self._records_for_ids(connection, namespace, exact_ids | semantic_ids)
        qualifying = []
        for row in rows:
            record_id = int(row["record_id"])
            exact = record_id in exact_ids
            semantic = record_id in semantic_ids and self._semantic_eligible(
                query_terms,
                self._terms(str(row["text"])),
                self.semantic_threshold,
            )
            if exact or semantic:
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
