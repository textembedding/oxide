from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from .journal import JournalClient, JournalError
from .workflow import WorkflowClient, WorkflowError
from .yaml_payload import YamlPayloadError, dump_yaml, load_single_string_field

PROTOCOL_VERSION = "2025-06-18"

TOOLS = (
    {
        "name": "journal_add",
        "description": (
            "Atomically add durable journal text. Queue claims and task completion are "
            "ordinary records interpreted by the workflow layer above the generic store."
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
            "Search journal text and current queue projections. Use queue:ready to seek "
            "work, worker:<slot> to resume, and task:<id> for durable context."
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


def _environment(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise JournalError(f"missing MCP environment: {name}")
    return value


class JournalMcpServer:
    def __init__(self) -> None:
        self.client = WorkflowClient(JournalClient(Path(_environment("SWARM_JOURNAL_SOCKET"))))
        self.run_id = _environment("SWARM_RUN_ID")
        self.worker_id = _environment("SWARM_WORKER_ID")

    def handle(self, request: Any) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        if "id" not in request:
            return None
        request_id = request.get("id")
        method = request.get("method")
        try:
            if method == "initialize":
                params = request.get("params")
                if (
                    not isinstance(params, dict)
                    or params.get("protocolVersion") != PROTOCOL_VERSION
                ):
                    raise JournalError("unsupported MCP protocol version")
                return self._result(
                    request_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "swarm-journal", "version": "1"},
                    },
                )
            if method == "ping":
                return self._result(request_id, {})
            if method == "tools/list":
                return self._result(request_id, {"tools": list(TOOLS)})
            if method == "tools/call":
                return self._result(request_id, self._call_tool(request.get("params")))
            return self._error(request_id, -32601, "Method not found")
        except (JournalError, WorkflowError, YamlPayloadError, ValueError, OSError) as error:
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
        arguments = params.get("arguments")
        if not isinstance(arguments, dict) or set(arguments) != {"yaml"}:
            raise ValueError("arguments must contain exactly one yaml field")
        if params.get("name") == "journal_add":
            text = load_single_string_field(arguments["yaml"], "text")
            result = self.client.add(self.run_id, self.worker_id, text)
        elif params.get("name") == "journal_search":
            query = load_single_string_field(arguments["yaml"], "query")
            result = {"matches": self.client.search(self.run_id, query)}
        else:
            raise ValueError("unknown tool")
        return {
            "content": [{"type": "text", "text": dump_yaml(result)}],
            "isError": False,
        }

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
            response = server.handle(json.loads(line))
        except json.JSONDecodeError:
            response = JournalMcpServer._error(None, -32700, "Parse error")
        if response is not None:
            sink.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")) + "\n")
            sink.flush()


def main() -> int:
    try:
        serve(JournalMcpServer())
    except (JournalError, OSError) as error:
        print(f"swarm-journal: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
