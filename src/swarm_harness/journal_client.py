"""Backend-neutral client for the local journal JSON protocol."""

from __future__ import annotations

import socket
import uuid
from pathlib import Path
from typing import Any

from .protocol import JournalError, ProtocolError, decode, encode


class JournalClient:
    def __init__(self, socket_path: str | Path, timeout: float = 10.0) -> None:
        self.socket_path = str(socket_path)
        self.timeout = timeout

    def call(self, operation: str, **arguments: Any) -> Any:
        request_id = uuid.uuid4().hex
        request = {
            "request_id": request_id,
            "operation": operation,
            "arguments": arguments,
        }
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(self.timeout)
            connection.connect(self.socket_path)
            connection.sendall(encode(request))
            stream = connection.makefile("rb")
            raw = stream.readline()
        if not raw:
            raise ProtocolError("journal closed without a response")
        response = decode(raw)
        if (
            not isinstance(response, dict)
            or response.get("request_id") != request_id
            or not isinstance(response.get("ok"), bool)
        ):
            raise ProtocolError("journal response has an invalid envelope")
        if response["ok"]:
            if "result" not in response:
                raise ProtocolError("successful response has no result")
            return response["result"]
        error = response.get("error")
        if not isinstance(error, dict):
            raise ProtocolError("failed response has no error")
        raise JournalError(str(error.get("code")), str(error.get("message")))

    def claim_task(
        self,
        run_id: str,
        worker_id: str,
        lease_seconds: float | None = None,
    ) -> dict:
        arguments: dict[str, Any] = {
            "run_id": run_id,
            "worker_id": worker_id,
            "ownership_mode": "lease" if lease_seconds is not None else "observable",
        }
        if lease_seconds is not None:
            arguments["lease_seconds"] = lease_seconds
        return self.call("claim_task", **arguments)

    def claim_work(
        self,
        run_id: str,
        worker_id: str,
        lease_seconds: float | None = None,
    ) -> dict:
        arguments: dict[str, Any] = {
            "run_id": run_id,
            "worker_id": worker_id,
            "ownership_mode": "lease" if lease_seconds is not None else "observable",
        }
        if lease_seconds is not None:
            arguments["lease_seconds"] = lease_seconds
        return self.call("claim_work", **arguments)

    def submit_result(self, **result: Any) -> dict:
        return self.call("submit_result", **result)

    def submit_validation(self, **validation: Any) -> dict:
        return self.call("submit_validation", **validation)

    def run_status(self, run_id: str) -> dict:
        return self.call("run_status", run_id=run_id)
