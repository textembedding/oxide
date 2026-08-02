"""Implementation-neutral newline-delimited JSON journal protocol."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ProtocolError(ValueError):
    """A malformed request or response crossed the journal boundary."""


class JournalError(RuntimeError):
    """The journal rejected a well-formed operation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Request:
    request_id: str
    operation: str
    arguments: dict[str, Any]

    @classmethod
    def from_value(cls, value: object) -> Request:
        if not isinstance(value, dict) or set(value) != {
            "request_id",
            "operation",
            "arguments",
        }:
            raise ProtocolError("request must have the closed v1 shape")
        request_id = value["request_id"]
        operation = value["operation"]
        arguments = value["arguments"]
        if not isinstance(request_id, str) or not request_id:
            raise ProtocolError("request_id must be a nonempty string")
        if not isinstance(operation, str) or not operation:
            raise ProtocolError("operation must be a nonempty string")
        if not isinstance(arguments, dict):
            raise ProtocolError("arguments must be an object")
        return cls(request_id, operation, arguments)


def require_string(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"{key} must be a nonempty string")
    return value


def require_number(values: Mapping[str, Any], key: str) -> float:
    value = values.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{key} must be a number")
    return float(value)


def encode(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def decode(raw: bytes) -> object:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProtocolError("message must be one UTF-8 JSON value") from error


def ok(request_id: str, result: object) -> dict[str, object]:
    return {"request_id": request_id, "ok": True, "result": result}


def failure(request_id: str, code: str, message: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "ok": False,
        "error": {"code": code, "message": message},
    }
