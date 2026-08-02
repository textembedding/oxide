"""Mandatory two-tool MCP facade for one fenced Codex worker invocation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from .journal_client import JournalClient
from .protocol import JournalError, ProtocolError
from .yaml_payload import YamlPayloadError, dump_yaml, load_single_string_field

PROTOCOL_VERSION = "2025-06-18"

TOOLS = (
    {
        "name": "journal_add",
        "description": (
            "Persist a durable observation, checkpoint, or handoff. Include stable task "
            "and artifact handles. Free text never mutates task lifecycle state."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "yaml": {
                    "type": "string",
                    "minLength": 1,
                    "description": "YAML containing exactly one nonempty string field named text.",
                }
            },
            "required": ["yaml"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": "journal_search",
        "description": (
            "Search authorized durable journal entries by a literal string to reconstruct "
            "task context and prior handoffs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "yaml": {
                    "type": "string",
                    "minLength": 1,
                    "description": "YAML containing exactly one nonempty string field named query.",
                }
            },
            "required": ["yaml"],
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    },
)


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"missing MCP environment: {name}")
    return value


class JournalMcpServer:
    def __init__(self) -> None:
        self.client = JournalClient(Path(_required_environment("SWARM_JOURNAL_SOCKET")))
        self.binding = {
            "run_id": _required_environment("SWARM_RUN_ID"),
            "worker_id": _required_environment("SWARM_WORKER_ID"),
            "task_id": _required_environment("SWARM_TASK_ID"),
            "claim_token": _required_environment("SWARM_CLAIM_TOKEN"),
        }

    def handle(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request.get("method")
        if "id" not in request:
            return None
        try:
            if method == "initialize":
                params = request.get("params")
                if (
                    not isinstance(params, dict)
                    or params.get("protocolVersion") != PROTOCOL_VERSION
                ):
                    raise ValueError("unsupported MCP protocol version")
                return self._result(
                    request_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": "swarm-harness-journal",
                            "version": "1.0.0",
                        },
                    },
                )
            if method == "ping":
                return self._result(request_id, {})
            if method == "tools/list":
                return self._result(request_id, {"tools": list(TOOLS)})
            if method == "tools/call":
                return self._result(request_id, self._call_tool(request.get("params")))
            return self._error(request_id, -32601, "Method not found")
        except (JournalError, ProtocolError, YamlPayloadError, ValueError, OSError) as error:
            if method == "tools/call":
                return self._result(
                    request_id,
                    {
                        "content": [{"type": "text", "text": dump_yaml({"error": str(error)})}],
                        "isError": True,
                    },
                )
            return self._error(request_id, -32602, str(error))

    def _call_tool(self, params: Any) -> dict[str, Any]:
        if not isinstance(params, dict) or set(params) - {"name", "arguments", "_meta"}:
            raise ValueError("tools/call has invalid fields")
        if not {"name", "arguments"} <= set(params):
            raise ValueError("tools/call requires name and arguments")
        arguments = params["arguments"]
        if not isinstance(arguments, dict) or set(arguments) != {"yaml"}:
            raise ValueError("arguments must contain exactly one yaml field")
        if params["name"] == "journal_add":
            text = load_single_string_field(arguments["yaml"], "text")
            result = self.client.call("journal_add", **self.binding, text=text)
            return {
                "content": [{"type": "text", "text": dump_yaml(result)}],
                "isError": False,
            }
        if params["name"] == "journal_search":
            query = load_single_string_field(arguments["yaml"], "query")
            matches = self.client.call("journal_search", **self.binding, query=query)
            return {
                "content": [{"type": "text", "text": dump_yaml({"matches": matches})}],
                "isError": False,
            }
        raise ValueError("unknown tool")

    @staticmethod
    def _result(request_id: Any, result: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


def serve(server: JournalMcpServer, source: TextIO = sys.stdin, sink: TextIO = sys.stdout) -> None:
    for line in source:
        try:
            request = json.loads(line)
            response = server.handle(request)
        except json.JSONDecodeError:
            response = JournalMcpServer._error(None, -32700, "Parse error")
        if response is not None:
            sink.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sink.flush()


def main() -> int:
    try:
        server = JournalMcpServer()
    except (ValueError, OSError) as error:
        print(f"swarm-harness-journal: {error}", file=sys.stderr)
        return 2
    serve(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
