"""Host-generation transport for one warm, journal-derived workflow projection."""

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
    """Serialize access to one derived projection shared by a launcher generation."""

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

    def projection_snapshot(
        self, namespace: str, after_record_id: int | None = 0
    ) -> dict[str, Any]:
        with self._lock:
            return self._client.projection_snapshot(namespace, after_record_id)


class ProjectionTransportError(WorkflowError):
    """The disposable host projection is unavailable or malformed."""


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
            namespace = str(arguments.get("namespace", ""))
            if self.server.run_id is not None and (  # type: ignore[attr-defined]
                namespace != self.server.run_id  # type: ignore[attr-defined]
                or request.get("epoch") != self.server.epoch  # type: ignore[attr-defined]
            ):
                raise WorkflowError("projection request belongs to another run epoch")
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
            elif operation == "worker_snapshot":
                result = self.server.client.worker_snapshot(  # type: ignore[attr-defined]
                    namespace,
                    str(arguments["worker"]),
                )
            elif operation == "projection_snapshot":
                after_record_id = arguments.get("after_record_id", 0)
                if after_record_id is not None:
                    after_record_id = int(after_record_id)
                result = self.server.client.projection_snapshot(  # type: ignore[attr-defined]
                    namespace, after_record_id
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
    request_queue_size = 64

    def __init__(
        self,
        socket_path: str | Path,
        client: SynchronizedWorkflowClient,
        *,
        run_id: str | None = None,
        epoch: int | None = None,
    ) -> None:
        self.socket_path = Path(socket_path)
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.client = client
        self.run_id = run_id
        self.epoch = epoch
        self.generation_id = secrets.token_hex(16)
        super().__init__(str(self.socket_path), _Handler)

    def server_close(self) -> None:
        super().server_close()
        self.socket_path.unlink(missing_ok=True)


class WorkflowProjectionClient:
    """Use the launcher generation's warm projection through its private Unix socket."""

    def __init__(
        self,
        socket_path: str | Path,
        timeout: float = 60.0,
        *,
        run_id: str | None = None,
        epoch: int | None = None,
    ) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout
        self.run_id = run_id
        self.epoch = epoch

    def _call(self, operation: str, arguments: dict[str, Any]) -> Any:
        request_id = secrets.token_hex(16)
        request = {
            "request_id": request_id,
            "operation": operation,
            "arguments": arguments,
            "epoch": self.epoch,
        }
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self.timeout)
                connection.connect(self.socket_path)
                connection.sendall(_encode(request))
                raw = connection.makefile("rb").readline()
            response = json.loads(raw)
        except (OSError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise ProjectionTransportError(f"host projection transport failed: {error}") from error
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            raise ProjectionTransportError("host projection returned an invalid response")
        if not response.get("ok"):
            raise WorkflowError(str(response.get("error", "worker projection rejected request")))
        return response.get("result")

    def add(self, namespace: str, author: str, text: str) -> dict[str, Any]:
        self._validate_namespace(namespace)
        result = self._call("add", {"namespace": namespace, "author": author, "text": text})
        if not isinstance(result, dict):
            raise WorkflowError("worker projection add returned an invalid result")
        return result

    def search(self, namespace: str, query: str) -> list[dict[str, Any]]:
        self._validate_namespace(namespace)
        result = self._call("search", {"namespace": namespace, "query": query})
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise WorkflowError("worker projection search returned an invalid result")
        return result

    def _validate_namespace(self, namespace: str) -> None:
        if self.run_id is not None and namespace != self.run_id:
            raise WorkflowError("projection client belongs to another run")

    def worker_snapshot(
        self, namespace: str, worker: str
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        self._validate_namespace(namespace)
        result = self._call("worker_snapshot", {"namespace": namespace, "worker": worker})
        if (
            not isinstance(result, list)
            or len(result) != 3
            or not isinstance(result[0], str)
            or not isinstance(result[1], list)
            or not isinstance(result[2], list)
        ):
            raise WorkflowError("host projection worker snapshot is invalid")
        return result[0], result[1], result[2]

    def projection_snapshot(
        self, namespace: str, after_record_id: int | None = 0
    ) -> dict[str, Any]:
        self._validate_namespace(namespace)
        result = self._call(
            "projection_snapshot",
            {"namespace": namespace, "after_record_id": after_record_id},
        )
        if not isinstance(result, dict) or not isinstance(result.get("entries"), list):
            raise WorkflowError("host projection snapshot is invalid")
        return result


def serve_projection_in_thread(
    socket_path: str | Path,
    client: SynchronizedWorkflowClient,
    *,
    run_id: str | None = None,
    epoch: int | None = None,
) -> tuple[WorkflowProjectionServer, threading.Thread]:
    server = WorkflowProjectionServer(socket_path, client, run_id=run_id, epoch=epoch)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread
