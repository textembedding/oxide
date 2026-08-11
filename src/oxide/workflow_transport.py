"""Worker-local transport for a warm, journal-derived workflow projection."""

from __future__ import annotations

import json
import secrets
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any

from .journal_backend import JournalError
from .workflow import WorkflowClient, WorkflowError


def _encode(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class SynchronizedWorkflowClient:
    """Serialize access to one derived projection shared by a worker host."""

    def __init__(self, client: WorkflowClient) -> None:
        self._client = client
        self._lock = threading.RLock()

    def add(self, namespace: str, author: str, text: str) -> dict[str, Any]:
        with self._lock:
            return self._client.add(namespace, author, text)

    def search(self, namespace: str, query: str) -> list[dict[str, Any]]:
        with self._lock:
            return self._client.search(namespace, query)

    def worker_snapshot(
        self, namespace: str, worker: str
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        with self._lock:
            return self._client.worker_snapshot(namespace, worker)


class _Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        request_id = "unknown"
        try:
            request = json.loads(self.rfile.readline())
            if not isinstance(request, dict):
                raise WorkflowError("projection request must be an object")
            request_id = str(request.get("request_id", ""))
            operation = str(request.get("operation", ""))
            arguments = request.get("arguments")
            if not request_id or not isinstance(arguments, dict):
                raise WorkflowError("projection request envelope is invalid")
            if operation == "add":
                result = self.server.client.add(  # type: ignore[attr-defined]
                    str(arguments["namespace"]),
                    str(arguments["author"]),
                    str(arguments["text"]),
                )
            elif operation == "search":
                result = self.server.client.search(  # type: ignore[attr-defined]
                    str(arguments["namespace"]),
                    str(arguments["query"]),
                )
            else:
                raise WorkflowError("unknown projection operation")
            response = {"request_id": request_id, "ok": True, "result": result}
        except (
            JournalError,
            KeyError,
            TypeError,
            ValueError,
            WorkflowError,
            json.JSONDecodeError,
        ) as error:
            response = {"request_id": request_id, "ok": False, "error": str(error)}
        self.wfile.write(_encode(response))


class WorkflowProjectionServer(socketserver.ThreadingUnixStreamServer):
    """Expose a disposable projection without creating another authority."""

    daemon_threads = True
    request_queue_size = 16

    def __init__(self, socket_path: str | Path, client: SynchronizedWorkflowClient) -> None:
        self.socket_path = Path(socket_path)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.client = client
        super().__init__(str(self.socket_path), _Handler)

    def server_close(self) -> None:
        super().server_close()
        self.socket_path.unlink(missing_ok=True)


class WorkflowProjectionClient:
    """Use the worker host's warm projection through its private Unix socket."""

    def __init__(self, socket_path: str | Path, timeout: float = 60.0) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def _call(self, operation: str, arguments: dict[str, Any]) -> Any:
        request_id = secrets.token_hex(16)
        request = {"request_id": request_id, "operation": operation, "arguments": arguments}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                connection.sendall(_encode(request))
                raw = connection.makefile("rb").readline()
            response = json.loads(raw)
        except (OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise WorkflowError(f"worker projection transport failed: {error}") from error
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            raise WorkflowError("worker projection returned an invalid response")
        if not response.get("ok"):
            raise WorkflowError(str(response.get("error", "worker projection rejected request")))
        return response.get("result")

    def add(self, namespace: str, author: str, text: str) -> dict[str, Any]:
        result = self._call("add", {"namespace": namespace, "author": author, "text": text})
        if not isinstance(result, dict):
            raise WorkflowError("worker projection add returned an invalid result")
        return result

    def search(self, namespace: str, query: str) -> list[dict[str, Any]]:
        result = self._call("search", {"namespace": namespace, "query": query})
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise WorkflowError("worker projection search returned an invalid result")
        return result


def serve_projection_in_thread(
    socket_path: str | Path, client: SynchronizedWorkflowClient
) -> tuple[WorkflowProjectionServer, threading.Thread]:
    server = WorkflowProjectionServer(socket_path, client)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
