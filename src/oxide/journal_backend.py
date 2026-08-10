"""The harness-side port for interchangeable two-operation journal kernels."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

DEFAULT_MIN_EXACT = 5
DEFAULT_MAX_RESULTS = 10


class JournalError(RuntimeError):
    """The selected journal kernel or its fixed transport rejected an operation."""


class JournalTimeoutError(JournalError):
    """A journal request exceeded the transport deadline and may be retried."""


def validate_search_capacity(min_exact: int, max_results: int) -> tuple[int, int]:
    """Validate the authoritative-replay subset of the kernel capacity contract."""

    if (
        isinstance(min_exact, bool)
        or isinstance(max_results, bool)
        or not isinstance(min_exact, int)
        or not isinstance(max_results, int)
        or not 1 <= min_exact <= max_results
    ):
        raise JournalError("journal capacity requires 1 <= min_exact <= max_results")
    return min_exact, max_results


def _encode(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


class _SocketJournalClient:
    """Kernel-neutral client for the fixed journal_add/journal_search wire protocol."""

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
        except TimeoutError as error:
            raise JournalTimeoutError("journal transport timed out") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise JournalError(f"journal transport failed: {error}") from error
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            raise JournalError("journal returned an invalid response")
        if not response.get("ok"):
            raise JournalError(str(response.get("error", "journal rejected request")))
        return response.get("result")

    def add(self, namespace: str, author: str, text: str) -> dict[str, Any]:
        result = self._call("journal_add", {"namespace": namespace, "author": author, "text": text})
        if not isinstance(result, dict):
            raise JournalError("journal_add returned an invalid result")
        return result

    def search(self, namespace: str, query: str) -> list[dict[str, Any]]:
        result = self._call("journal_search", {"namespace": namespace, "query": query})
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise JournalError("journal_search returned an invalid result")
        return result


class JournalPort(Protocol):
    """Everything the workflow layer may know about a journal kernel."""

    def add(self, namespace: str, author: str, text: str) -> dict[str, Any]: ...

    def search(self, namespace: str, query: str) -> list[dict[str, Any]]: ...


class JournalRuntime:
    """A running kernel and its two-operation client."""

    def __init__(
        self,
        client: JournalPort,
        close: Callable[[], None],
        *,
        min_exact: int,
        max_results: int,
    ) -> None:
        self.client = client
        self._close = close
        self.min_exact = min_exact
        self.max_results = max_results

    def close(self) -> None:
        callback = self._close
        if callable(callback):
            callback()
            self._close = None


def connect_journal(socket_path: str | Path, *, timeout: float = 60.0) -> JournalPort:
    """Connect through the fixed journal_add/journal_search transport contract."""

    return _SocketJournalClient(socket_path, timeout=timeout)


def _wait_until_ready(socket_path: Path, process: subprocess.Popen[bytes], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise JournalError(f"journal kernel exited with status {process.returncode}")
        if socket_path.exists():
            try:
                connect_journal(socket_path, timeout=0.5).search(
                    "oxide-health", "oxide-health-probe"
                )
                return
            except (JournalError, OSError):
                pass
        time.sleep(0.05)
    raise JournalError(f"journal kernel did not become ready: {socket_path}")


def start_journal(
    database: str | Path,
    socket_path: str | Path,
    command: Sequence[str] | None = None,
    *,
    min_exact: int = DEFAULT_MIN_EXACT,
    max_results: int = DEFAULT_MAX_RESULTS,
    timeout: float = 30.0,
) -> JournalRuntime:
    """Start either the disposable Python prototype or a compatible external kernel."""

    min_exact, max_results = validate_search_capacity(min_exact, max_results)
    socket = Path(socket_path)
    if not command:
        # Keep the prototype and sqlite3 completely unloaded when an external kernel
        # is selected.  The workflow above this branch only sees JournalPort.
        from .journal import JournalError as PrototypeJournalError
        from .journal import serve_in_thread

        try:
            server, thread = serve_in_thread(
                database,
                socket,
                min_exact=min_exact,
                max_results=max_results,
            )
        except PrototypeJournalError as error:
            raise JournalError(str(error)) from error

        def close() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        return JournalRuntime(
            connect_journal(socket),
            close,
            min_exact=min_exact,
            max_results=max_results,
        )

    argv = [str(value) for value in command]
    executable = shutil.which(argv[0]) or argv[0]
    environment = os.environ.copy()
    environment.update(
        {
            "OXIDE_JOURNAL_DATABASE": str(Path(database).resolve()),
            "OXIDE_JOURNAL_SOCKET": str(socket.resolve()),
            "OXIDE_JOURNAL_MIN_EXACT": str(min_exact),
            "OXIDE_JOURNAL_MAX_RESULTS": str(max_results),
        }
    )
    socket.unlink(missing_ok=True)
    process = subprocess.Popen(
        [executable, *argv[1:]],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_until_ready(socket, process, timeout)
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        socket.unlink(missing_ok=True)
        raise

    def close() -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        socket.unlink(missing_ok=True)

    return JournalRuntime(
        connect_journal(socket),
        close,
        min_exact=min_exact,
        max_results=max_results,
    )
