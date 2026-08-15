"""Interactive planning and contract-generation sessions.

The adapters in this module keep the production CLI genuinely collaborative while
allowing model-free tests to drive exactly the same state machine.
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from typing import Any, Protocol, TextIO

from .alignment import (
    AlignmentError,
    validate_interactive_trace,
)
from .contract import ContractError, contract_payload_digest, validate_contract
from .prompt_templates import PromptTemplateError, render_prompt
from .roadmap import (
    RoadmapError,
    canonical_bytes,
    digest_bytes,
    load_roadmap,
    parse_roadmap,
    proposed_stage_binding,
    proposed_stage_set_binding,
    render_roadmap_document,
    roadmap_maintenance_impact,
    specification_corpus,
    stage_set_binding,
)
from .verification_policy import (
    POLICY_PROFILE,
    verification_policy_digest,
    verification_policy_prompt,
)


class PlanningError(RuntimeError):
    pass


def _render_agent_prompt(name: str, **values: object) -> str:
    try:
        return render_prompt(name, **values)
    except PromptTemplateError as error:
        raise PlanningError(str(error)) from error


DEFAULT_SESSION_MODEL = "gpt-5.6-sol"
DEFAULT_SESSION_REASONING_EFFORT = "max"
DEFAULT_SESSION_REASONING_SUMMARY = "detailed"


class SessionAgent(Protocol):
    @property
    def identity(self) -> str: ...

    def start(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]: ...

    def respond(self, feedback: str, schema: dict[str, Any]) -> dict[str, Any]: ...


class SessionUser(Protocol):
    def show(self, text: str) -> None: ...

    def respond(self, prompt: str) -> str: ...


class TerminalUser:
    def show(self, text: str) -> None:
        print(text)

    def respond(self, prompt: str) -> str:
        return input(prompt).strip()


class ScriptedAgent:
    """Deterministic adapter for regression transcripts."""

    def __init__(self, turns: Iterable[dict[str, Any]], identity: str = "fake-agent/test"):
        self._turns = iter(turns)
        self._identity = identity

    @property
    def identity(self) -> str:
        return self._identity

    def _next(self) -> dict[str, Any]:
        try:
            return next(self._turns)
        except StopIteration as error:
            raise PlanningError("scripted agent ran out of responses") from error

    def start(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        del prompt, schema
        return self._next()

    def respond(self, feedback: str, schema: dict[str, Any]) -> dict[str, Any]:
        del feedback, schema
        return self._next()


class ScriptedUser:
    """Deterministic user adapter retaining the displayed transcript."""

    def __init__(self, responses: Iterable[str]):
        self._responses = iter(responses)
        self.transcript: list[str] = []

    def show(self, text: str) -> None:
        self.transcript.append(text)

    def respond(self, prompt: str) -> str:
        self.transcript.append(prompt)
        try:
            response = next(self._responses)
        except StopIteration as error:
            raise PlanningError("scripted user ran out of responses") from error
        self.transcript.append(response)
        return response


def selectable_ready_phases(roadmap: dict[str, Any]) -> list[dict[str, Any]]:
    """Return selector metadata for every roadmap phase in roadmap order."""
    phases: list[dict[str, Any]] = []
    for phase in roadmap["stages"]:
        phases.append(
            {
                "id": phase["id"],
                "outcome": phase["outcome"],
                "readiness": phase["readiness"],
                "dependencies": phase["dependencies"],
            }
        )
    return phases


def select_contract_phases(roadmap: dict[str, Any], user: SessionUser) -> list[str]:
    """Interactive checkbox-style selector with dependency-aware toggling.

    A numeric toggle is intentionally used instead of a terminal UI dependency so
    the same behavior works over SSH and remains scriptable in model-free tests.
    """
    phases = selectable_ready_phases(roadmap)
    chosen: set[str] = set()
    while True:
        lines = ["Select ready phases for this contract:"]
        for index, phase in enumerate(phases, 1):
            dependencies = set(phase["dependencies"])
            checked = phase["id"] in chosen
            marker = "x" if checked else " "
            suffix = ""
            if phase["readiness"] != "ready":
                suffix = f" (not ready: {phase['readiness']})"
            elif not dependencies <= chosen:
                suffix = (
                    " (select dependencies first: "
                    + ", ".join(item for item in phase["dependencies"] if item not in chosen)
                    + ")"
                )
            lines.append(f"  {index}. [{marker}] {phase['id']} — {phase['outcome']}{suffix}")
        user.show("\n".join(lines))
        response = user.respond("Toggle a phase number, /confirm the checked phases, or /quit: ")
        if response == "/quit":
            raise PlanningError("contract-generation session cancelled; nothing was approved")
        if response == "/confirm":
            if not chosen:
                user.show("Select at least one ready phase before confirming.")
                continue
            return [phase["id"] for phase in phases if phase["id"] in chosen]
        try:
            index = int(response) - 1
            if not 0 <= index < len(phases):
                raise IndexError
            phase = phases[index]
        except (ValueError, IndexError):
            user.show("Enter one displayed phase number, /confirm, or /quit.")
            continue
        identifier = phase["id"]
        if identifier in chosen:
            dependents = {
                item["id"]
                for item in phases
                if item["id"] in chosen and identifier in item["dependencies"]
            }
            if dependents:
                user.show("Uncheck dependent phases first: " + ", ".join(sorted(dependents)))
                continue
            chosen.remove(identifier)
            continue
        missing = [item for item in phase["dependencies"] if item not in chosen]
        if phase["readiness"] != "ready":
            user.show(f"{identifier} is {phase['readiness']}, not ready.")
        elif missing:
            user.show("Select dependencies first: " + ", ".join(missing))
        else:
            chosen.add(identifier)


class CodexSessionAgent:
    """A read-only Codex CLI session using the caller's existing authentication."""

    _REASONING_EFFORTS = ("none", "low", "medium", "high", "xhigh", "max")

    def __init__(
        self,
        repository: Path,
        *,
        model: str | None = DEFAULT_SESSION_MODEL,
        reasoning_effort: str = DEFAULT_SESSION_REASONING_EFFORT,
        timeout_seconds: float = 900.0,
        heartbeat_seconds: float = 10.0,
        progress: Callable[[str], None] | None = None,
    ):
        executable = shutil.which("codex")
        if executable is None:
            raise PlanningError("codex CLI is not installed or is absent from PATH")
        if timeout_seconds <= 0 or heartbeat_seconds <= 0:
            raise PlanningError("Codex session timeout and heartbeat must be positive")
        if reasoning_effort not in self._REASONING_EFFORTS:
            choices = ", ".join(sorted(self._REASONING_EFFORTS))
            raise PlanningError(f"Codex reasoning effort must be one of: {choices}")
        self.repository = repository
        self.executable = executable
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.timeout_seconds = timeout_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self._progress = progress or self._terminal_progress
        self.thread_id: str | None = None

    @property
    def identity(self) -> str:
        suffix = self.thread_id or "unstarted"
        return f"codex/{suffix}"

    @staticmethod
    def _terminal_progress(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    @staticmethod
    def _read_stream(
        name: str,
        stream: TextIO,
        events: Queue[tuple[str, str | None, float]],
    ) -> None:
        try:
            for line in stream:
                events.put((name, line.rstrip("\n"), time.monotonic()))
        finally:
            stream.close()
            events.put((name, None, time.monotonic()))

    @staticmethod
    def _stop_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=2)

    @staticmethod
    def _event_progress(event: object) -> str | None:
        if not isinstance(event, dict):
            return None
        event_type = event.get("type")
        if event_type == "thread.started":
            thread_id = event.get("thread_id")
            suffix = str(thread_id)[-8:] if thread_id else "unknown"
            return f"Codex session started ({suffix})."
        if event_type == "turn.started":
            return "Codex is reading the specifications and drafting a proposal."
        if event_type == "turn.completed":
            return "Codex finished the current proposal."
        if event_type not in {"item.started", "item.completed"}:
            return None
        item = event.get("item")
        if not isinstance(item, dict):
            return None
        item_type = str(item.get("type", "work"))
        phase = "Started" if event_type == "item.started" else "Finished"
        if item_type == "command_execution":
            command = item.get("command")
            if isinstance(command, str) and command.strip():
                compact = " ".join(command.split())
                if len(compact) > 120:
                    compact = compact[:117] + "..."
                return f"{phase} repository inspection: {compact}"
            return f"{phase} repository inspection."
        if item_type in {"mcp_tool_call", "tool_call"}:
            tool = item.get("tool") or item.get("name") or "tool"
            return f"{phase} {tool}."
        if item_type == "reasoning" and event_type == "item.completed":
            summaries: list[str] = []
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                summaries.append(text)
            summary = item.get("summary")
            if isinstance(summary, str) and summary.strip():
                summaries.append(summary)
            elif isinstance(summary, list):
                for part in summary:
                    if isinstance(part, str) and part.strip():
                        summaries.append(part)
                    elif isinstance(part, dict):
                        part_text = part.get("text")
                        if isinstance(part_text, str) and part_text.strip():
                            summaries.append(part_text)
            if summaries:
                compact = " / ".join(" ".join(value.split()) for value in summaries)
                if len(compact) > 480:
                    compact = compact[:477] + "..."
                return f"Codex reasoning: {compact}"
            return "Codex completed a reasoning step."
        if item_type in {"agent_message", "message"} and event_type == "item.completed":
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                try:
                    structured = json.loads(text)
                except json.JSONDecodeError:
                    structured = None
                if isinstance(structured, dict) and isinstance(structured.get("message"), str):
                    text = structured["message"]
                compact = " ".join(text.split())
                if len(compact) > 240:
                    compact = compact[:237] + "..."
                return f"Codex progress: {compact}"
            return "Codex reported progress."
        return None

    def _run(self, prompt: str, schema: dict[str, Any], *, resume: bool) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="oxide-session-") as directory:
            root = Path(directory)
            schema_path = root / "output.schema.json"
            output_path = root / "response.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            if resume:
                if self.thread_id is None:
                    raise PlanningError("cannot resume an unstarted Codex session")
                command = [
                    self.executable,
                    "exec",
                    "resume",
                    "--config",
                    f'model_reasoning_effort="{self.reasoning_effort}"',
                    "--config",
                    f'model_reasoning_summary="{DEFAULT_SESSION_REASONING_SUMMARY}"',
                    "--json",
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                    self.thread_id,
                    "-",
                ]
            else:
                command = [
                    self.executable,
                    "exec",
                    "--config",
                    f'model_reasoning_effort="{self.reasoning_effort}"',
                    "--config",
                    f'model_reasoning_summary="{DEFAULT_SESSION_REASONING_SUMMARY}"',
                    "--json",
                    "--sandbox",
                    "read-only",
                    "-C",
                    str(self.repository),
                    "--output-schema",
                    str(schema_path),
                    "--output-last-message",
                    str(output_path),
                ]
                if self.model:
                    command.extend(["--model", self.model])
                command.append("-")
            self._progress(
                "Starting Codex agent turn "
                f"(reasoning: {self.reasoning_effort}). "
                "No files will be changed before your approval."
            )
            try:
                process = subprocess.Popen(
                    command,
                    cwd=self.repository,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
            except OSError as error:
                raise PlanningError(f"could not start Codex planning session: {error}") from error
            assert (
                process.stdin is not None
                and process.stdout is not None
                and process.stderr is not None
            )
            events: Queue[tuple[str, str | None, float]] = Queue()
            readers = [
                Thread(
                    target=self._read_stream,
                    args=(name, stream, events),
                    daemon=True,
                )
                for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
            ]
            for reader in readers:
                reader.start()
            try:
                process.stdin.write(prompt)
                process.stdin.close()
            except (BrokenPipeError, OSError) as error:
                self._stop_process(process)
                raise PlanningError(
                    f"could not deliver the planning prompt to Codex: {error}"
                ) from error
            started = time.monotonic()
            last_activity = started
            next_heartbeat = started + self.heartbeat_seconds
            open_streams = len(readers)
            diagnostic_tail: deque[str] = deque(maxlen=40)
            try:
                while open_streams:
                    now = time.monotonic()
                    deadline = last_activity + self.timeout_seconds
                    wait = max(
                        0.0,
                        min(0.5, deadline - now, max(0.0, next_heartbeat - now)),
                    )
                    try:
                        stream_name, line, observed_at = events.get(timeout=wait)
                    except Empty:
                        now = time.monotonic()
                        if now >= last_activity + self.timeout_seconds:
                            self._stop_process(process)
                            raise PlanningError(
                                "Codex agent turn produced no events for "
                                f"{self.timeout_seconds:g} seconds "
                                f"({int(now - started)}s total); no response was approved"
                            )
                        if now >= next_heartbeat:
                            elapsed = int(now - started)
                            idle = int(now - last_activity)
                            self._progress(
                                "Codex is still running "
                                f"({elapsed}s elapsed; {idle}s since the last event)."
                            )
                            next_heartbeat = now + self.heartbeat_seconds
                        continue
                    if line is None:
                        open_streams -= 1
                        continue
                    last_activity = max(last_activity, observed_at)
                    diagnostic_tail.append(line[:2000])
                    if stream_name == "stderr":
                        compact = " ".join(line.split())
                        if compact:
                            self._progress("Codex: " + compact[:240])
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict) and event.get("type") == "thread.started":
                        thread_id = event.get("thread_id")
                        if isinstance(thread_id, str):
                            self.thread_id = thread_id
                    progress = self._event_progress(event)
                    if progress:
                        self._progress(progress)
            except KeyboardInterrupt as error:
                self._stop_process(process)
                raise PlanningError(
                    "Codex planning session interrupted; no proposal was approved"
                ) from error
            remaining = max(0.0, last_activity + self.timeout_seconds - time.monotonic())
            try:
                returncode = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._stop_process(process)
                raise PlanningError(
                    "Codex agent turn produced no events for "
                    f"{self.timeout_seconds:g} seconds; no response was approved"
                ) from None
            if returncode:
                detail = "\n".join(diagnostic_tail).strip() or f"exit status {returncode}"
                raise PlanningError(f"Codex planning session failed: {detail}")
            try:
                response = json.loads(output_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise PlanningError(
                    "Codex returned no valid structured planning response"
                ) from error
            if not isinstance(response, dict):
                raise PlanningError("Codex planning response must be an object")
            return response

    def start(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        return self._run(prompt, schema, resume=False)

    def respond(self, feedback: str, schema: dict[str, Any]) -> dict[str, Any]:
        return self._run(feedback, schema, resume=True)


PLAN_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "message",
        "ready_for_approval",
        "complete_specification_corpus",
        "faithful_to_specifications",
        "unresolved",
        "roadmap_markdown",
    ],
    "properties": {
        "message": {"type": "string"},
        "ready_for_approval": {"type": "boolean"},
        "complete_specification_corpus": {"type": "boolean"},
        "faithful_to_specifications": {"type": "boolean"},
        "unresolved": {"type": "array", "items": {"type": "string"}},
        "roadmap_markdown": {"type": "string"},
    },
}

# Kept private at the workflow call sites; evaluators import the stable public name.
_PLAN_SCHEMA = PLAN_RESPONSE_SCHEMA


_GAPS_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "ambiguities",
        "missing_acceptance_criteria",
        "unsupported_assumptions",
        "semantic_gaps",
    ],
    "properties": {
        key: {"type": "array", "items": {"type": "string"}}
        for key in (
            "ambiguities",
            "missing_acceptance_criteria",
            "unsupported_assumptions",
            "semantic_gaps",
        )
    },
}


_CONTRACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "message",
        "ready_for_approval",
        "contractible",
        "faithful_to_sources",
        "complete_specification_corpus",
        "unresolved",
        "verification_goals",
        "file_updates",
        "roadmap_markdown",
        "contract_toml",
    ],
    "properties": {
        "message": {"type": "string"},
        "ready_for_approval": {"type": "boolean"},
        "contractible": {"type": "boolean"},
        "faithful_to_sources": {"type": "boolean"},
        "complete_specification_corpus": {"type": "boolean"},
        "unresolved": _GAPS_SCHEMA,
        "verification_goals": {"type": "array", "items": {"type": "string"}},
        "file_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["path", "content", "reason"],
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
        "roadmap_markdown": {"type": "string"},
        "contract_toml": {"type": "string"},
    },
}


def _git_root(path: Path) -> Path:
    location = path.parent if path.is_file() else path
    result = subprocess.run(
        ["git", "-C", str(location), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise PlanningError("target must be inside a Git worktree")
    return Path(result.stdout.strip()).resolve()


def git_identity(repository: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for field, key in (("name", "user.name"), ("email", "user.email")):
        result = subprocess.run(
            ["git", "-C", str(repository), "config", key],
            text=True,
            capture_output=True,
            check=False,
        )
        values[field] = result.stdout.strip()
    if not values["name"] or "@" not in values["email"]:
        raise PlanningError("target repository must configure Git user.name and user.email")
    return values


def _proposal_diff(path: Path, proposed: str) -> str:
    before = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    after = proposed.splitlines()
    return "\n".join(
        difflib.unified_diff(
            before,
            after,
            fromfile=str(path),
            tofile=str(path),
            lineterm="",
            n=3,
        )
    )


def _maintenance_impact_text(impact: dict[str, Any]) -> str:
    lines = ["Maintenance impact:"]
    lines.extend(
        f"- {change['stage_id']}: {', '.join(change['fields'])}" for change in impact["changes"]
    )
    dependent = impact["dependent_stage_ids"]
    if dependent:
        lines.append("- dependent phase approvals invalidated: " + ", ".join(dependent))
    else:
        lines.append("- no dependent phase approvals invalidated")
    lines.append(f"- {len(impact['preserved_stage_ids'])} unaffected phase approvals preserved")
    return "\n".join(lines)


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".tmp-{os.getpid()}")
    temporary.write_text(_normalized_text(content), encoding="utf-8")
    temporary.replace(path)


def _normalized_text(content: str) -> str:
    return content.rstrip() + "\n"


def _frozen_source_bundle(repository: Path, paths: Iterable[str]) -> str:
    blocks: list[str] = []
    seen: set[str] = set()
    root = repository.resolve()
    for value in paths:
        relative = Path(value).as_posix()
        if relative in seen:
            continue
        seen.add(relative)
        path = (root / relative).resolve()
        if not path.is_file() or not path.is_relative_to(root):
            raise PlanningError(f"planning source is absent or escapes the repository: {relative}")
        data = path.read_bytes()
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PlanningError(f"planning source is not UTF-8: {relative}") from error
        metadata = json.dumps(
            {"path": relative, "sha256": digest_bytes(data)},
            sort_keys=True,
            separators=(",", ":"),
        )
        blocks.append(
            f"===== BEGIN OXIDE SOURCE {metadata} =====\n"
            + content.rstrip()
            + f"\n===== END OXIDE SOURCE {relative} ====="
        )
    return "\n\n".join(blocks)


def planning_prompt_values(
    repository: Path,
    specification_directory: str,
    *,
    maintenance_stage_ids: Iterable[str] = (),
    maintenance_request: str = "",
) -> dict[str, object]:
    """Return the frozen inputs used by production and prompt evaluation."""
    corpus = specification_corpus(repository, specification_directory)
    existing = [path for path in ("ROADMAP.md", "docs/ROADMAP.md") if (repository / path).is_file()]
    bundle = _frozen_source_bundle(
        repository,
        [*(item["path"] for item in corpus), *existing],
    )
    policy = verification_policy_prompt()
    maintenance_ids = list(maintenance_stage_ids)
    return {
        "specification_directory_json": json.dumps(specification_directory),
        "corpus_json": json.dumps(corpus, sort_keys=True),
        "policy_profile_json": json.dumps(POLICY_PROFILE),
        "policy_digest_json": json.dumps(verification_policy_digest()),
        "maintenance_mode": bool(maintenance_ids),
        "maintenance_phase_ids_json": json.dumps(maintenance_ids),
        "maintenance_request": maintenance_request,
        "source_bundle": bundle,
        "verification_policy": policy,
    }


def _plan_prompt(
    repository: Path,
    specification_directory: str,
    *,
    maintenance_stage_ids: Iterable[str] = (),
    maintenance_request: str = "",
) -> str:
    return _render_agent_prompt(
        "planning",
        **planning_prompt_values(
            repository,
            specification_directory,
            maintenance_stage_ids=maintenance_stage_ids,
            maintenance_request=maintenance_request,
        ),
    )


def run_plan_session(
    specification_path: Path,
    *,
    agent: SessionAgent,
    user: SessionUser,
    user_identity: dict[str, str] | None = None,
    update_stage_ids: Iterable[str] = (),
) -> Path:
    repository = _git_root(specification_path)
    directory = specification_path.resolve().relative_to(repository).as_posix()
    try:
        specification_corpus(repository, directory)
    except RoadmapError as error:
        raise PlanningError(str(error)) from error
    roadmap_path = repository / "ROADMAP.md"
    maintenance_ids = list(update_stage_ids)
    if len(maintenance_ids) != len(set(maintenance_ids)):
        raise PlanningError("roadmap maintenance phase IDs must be unique")
    baseline: dict[str, Any] | None = None
    maintenance_request = ""
    if maintenance_ids:
        if not roadmap_path.is_file():
            raise PlanningError("roadmap maintenance requires an existing approved ROADMAP.md")
        try:
            baseline = load_roadmap(roadmap_path)
            if baseline["status"] != "ready":
                raise RoadmapError("roadmap maintenance requires a ready baseline")
        except RoadmapError as error:
            raise PlanningError(f"roadmap maintenance baseline is not approved: {error}") from error
        while not maintenance_request:
            maintenance_request = user.respond(
                "Describe the change for " + ", ".join(maintenance_ids) + " (or /quit): "
            ).strip()
            if maintenance_request == "/quit":
                raise PlanningError("roadmap maintenance cancelled; nothing was changed")
            if not maintenance_request:
                user.show("Maintenance requires a concrete change request.")
    response = agent.start(
        _plan_prompt(
            repository,
            directory,
            maintenance_stage_ids=maintenance_ids,
            maintenance_request=maintenance_request,
        ),
        _PLAN_SCHEMA,
    )
    mechanical_repairs = 0
    while True:
        roadmap_problem = ""
        maintenance_impact: dict[str, Any] | None = None
        message = str(response.get("message", "")).strip()
        user.show(message or "The planning agent returned no explanation.")
        roadmap_text = response.get("roadmap_markdown")
        if isinstance(roadmap_text, str) and roadmap_text.strip():
            try:
                roadmap_text = render_roadmap_document(roadmap_text, roadmap_path)
                roadmap = parse_roadmap(roadmap_text, roadmap_path)
            except RoadmapError as error:
                roadmap = None
                roadmap_problem = str(error)
                mechanical_repairs += 1
                user.show(
                    "The planning agent returned an invalid ROADMAP.md structure; "
                    "Oxide is asking it to repair the format before requesting your review. "
                    f"Nothing has been written. Error: {roadmap_problem}"
                )
                if mechanical_repairs > 3:
                    raise PlanningError(
                        "planning agent could not produce a mechanically valid ROADMAP.md "
                        f"after 3 automatic repairs: {roadmap_problem}"
                    ) from error
                response = agent.respond(
                    _render_agent_prompt(
                        "planning-follow-up",
                        kind="schema-repair",
                        problem=roadmap_problem,
                    ),
                    _PLAN_SCHEMA,
                )
                continue
            try:
                for stage in roadmap["stages"]:
                    proposed_stage_binding(
                        repository,
                        "ROADMAP.md",
                        roadmap_text,
                        stage["id"],
                        {},
                    )
                if baseline is not None:
                    maintenance_impact = roadmap_maintenance_impact(
                        baseline, roadmap, maintenance_ids
                    )
                    user.show(_maintenance_impact_text(maintenance_impact))
                user.show(
                    "Proposed stages: " + ", ".join(stage["id"] for stage in roadmap["stages"])
                )
                diff = _proposal_diff(roadmap_path, roadmap_text)
                if diff:
                    user.show("Proposed ROADMAP.md (not written until you approve):")
                    user.show(diff)
            except RoadmapError as error:
                roadmap = None
                roadmap_problem = str(error)
                mechanical_repairs += 1
                user.show(
                    "The planning agent returned a roadmap that failed source-trace or "
                    "maintenance qualification; Oxide is asking it to repair the proposal "
                    "before requesting your review. Nothing has been written. "
                    f"Error: {roadmap_problem}"
                )
                if mechanical_repairs > 3:
                    raise PlanningError(
                        "planning agent could not produce a qualified ROADMAP.md after "
                        f"3 automatic repairs: {roadmap_problem}"
                    ) from error
                response = agent.respond(
                    _render_agent_prompt(
                        "planning-follow-up",
                        kind="trace-repair",
                        problem=roadmap_problem,
                    ),
                    _PLAN_SCHEMA,
                )
                continue
        else:
            roadmap = None
            roadmap_problem = "the planning agent did not return a complete ROADMAP.md"
            mechanical_repairs += 1
            user.show(
                "The planning agent omitted ROADMAP.md; Oxide is requesting a complete "
                "proposal before asking for your review. Nothing has been written."
            )
            if mechanical_repairs > 3:
                raise PlanningError(
                    "planning agent did not return a complete ROADMAP.md after 3 automatic repairs"
                )
            response = agent.respond(
                _render_agent_prompt("planning-follow-up", kind="missing-roadmap"),
                _PLAN_SCHEMA,
            )
            continue
        mechanical_repairs = 0
        decision = user.respond("Feedback, /approve this exact roadmap, or /quit: ")
        if decision == "/quit":
            raise PlanningError("planning session cancelled; no roadmap was approved")
        if decision != "/approve":
            response = agent.respond(
                _render_agent_prompt(
                    "planning-follow-up",
                    kind="user-feedback",
                    feedback=decision,
                    problem=roadmap_problem or "none",
                ),
                _PLAN_SCHEMA,
            )
            continue
        unresolved = response.get("unresolved")
        if (
            roadmap is None
            or response.get("ready_for_approval") is not True
            or response.get("complete_specification_corpus") is not True
            or response.get("faithful_to_specifications") is not True
            or unresolved != []
            or roadmap["status"] != "ready"
        ):
            user.show("Approval denied: the roadmap is not aligned and mechanically ready.")
            response = agent.respond(
                _render_agent_prompt(
                    "planning-follow-up",
                    kind="approval-denied",
                    problem=roadmap_problem or "none",
                ),
                _PLAN_SCHEMA,
            )
            continue
        try:
            _apply_with_rollback(
                {roadmap_path: roadmap_text},
                lambda: None,
            )
        except RoadmapError as error:
            raise PlanningError(f"approved roadmap failed qualification: {error}") from error
        user.show(f"Approved roadmap written to {roadmap_path}")
        return roadmap_path


def _contract_prompt(repository: Path, roadmap_path: str, stage_ids: Iterable[str]) -> str:
    selected = [stage_ids] if isinstance(stage_ids, str) else list(stage_ids)
    binding = stage_set_binding(repository, roadmap_path, selected)
    source_paths = sorted({item["path"] for item in binding["semantic_closure"]})
    bundle = _frozen_source_bundle(repository, [roadmap_path, *source_paths])
    policy = verification_policy_prompt()
    return _render_agent_prompt(
        "contract-generation",
        selected_phases_json=json.dumps(selected),
        roadmap_path_json=json.dumps(roadmap_path),
        semantic_closure_json=json.dumps(binding["semantic_closure"], indent=2, sort_keys=True),
        policy_profile_json=json.dumps(POLICY_PROFILE),
        policy_digest_json=json.dumps(verification_policy_digest()),
        source_bundle=bundle,
        verification_policy=policy,
    )


def _contract_value(text: str, *, require_approval: bool = False) -> dict[str, Any]:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PlanningError(f"generated contract TOML is invalid: {error}") from error
    try:
        return validate_contract(raw, require_approval=require_approval)
    except ContractError as error:
        raise PlanningError(str(error)) from error


def _validated_updates(
    repository: Path,
    specification_root: str,
    response: dict[str, Any],
) -> dict[Path, str]:
    updates: dict[Path, str] = {}
    root = (repository / specification_root).resolve()
    raw_updates = response.get("file_updates")
    if not isinstance(raw_updates, list):
        raise PlanningError("contract agent file updates are malformed")
    for item in raw_updates:
        if not isinstance(item, dict) or set(item) != {"path", "content", "reason"}:
            raise PlanningError("contract agent file update is malformed")
        path = (repository / str(item["path"])).resolve()
        if not path.is_relative_to(root) or path.suffix.lower() != ".md":
            raise PlanningError("contract agent may update only Markdown source specifications")
        if path in updates or not isinstance(item["content"], str):
            raise PlanningError("contract agent proposed duplicate or malformed source updates")
        updates[path] = item["content"]
    roadmap = response.get("roadmap_markdown")
    contract = response.get("contract_toml")
    if not isinstance(roadmap, str) or not isinstance(contract, str):
        raise PlanningError("contract agent did not return complete derived artifacts")
    updates[repository / "ROADMAP.md"] = render_roadmap_document(roadmap, "ROADMAP.md")
    updates[repository / "verification" / "contract.toml"] = contract
    return updates


def _toml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def embed_contract_approval(
    contract_text: str,
    *,
    binding: dict[str, Any],
    agent_identity: str,
    user_identity: dict[str, str],
) -> str:
    """Append the actual interactive decisions to an agent-generated contract."""
    try:
        raw = tomllib.loads(contract_text)
    except tomllib.TOMLDecodeError as error:
        raise PlanningError(f"generated contract TOML is invalid: {error}") from error
    if raw.get("schema") != 5:
        raise PlanningError("interactive multi-phase generation requires contract schema 5")
    if {"binding", "attestation", "approval"} & set(raw):
        raise PlanningError("Oxide, not the contract agent, owns approval metadata")
    semantic_binding = {
        "stage_set_sha256": binding["stage_set_sha256"],
        "global_invariants_sha256": binding["global_invariants_sha256"],
        "semantic_closure_sha256": binding["semantic_closure_sha256"],
    }
    raw["binding"] = semantic_binding
    payload_sha256 = contract_payload_digest(raw)
    metadata = (
        "\n\n[binding]\n"
        f"stage_set_sha256 = {_toml_quote(semantic_binding['stage_set_sha256'])}\n"
        "global_invariants_sha256 = "
        f"{_toml_quote(semantic_binding['global_invariants_sha256'])}\n"
        "semantic_closure_sha256 = "
        f"{_toml_quote(semantic_binding['semantic_closure_sha256'])}\n\n"
        "[attestation]\n"
        f"identity = {_toml_quote(agent_identity)}\n"
        f"payload_sha256 = {_toml_quote(payload_sha256)}\n"
        "contractible = true\n"
        "faithful_to_approved_sources = true\n"
        "introduces_no_product_semantics = true\n"
        "unresolved = []\n\n"
        "[approval]\n"
        f"user_name = {_toml_quote(user_identity['name'])}\n"
        f"user_email = {_toml_quote(user_identity['email'])}\n"
        f"payload_sha256 = {_toml_quote(payload_sha256)}\n"
        "approved = true\n"
    )
    return contract_text.rstrip() + metadata


def _apply_with_rollback(
    updates: dict[Path, str], validate: Any, *, tracked: Iterable[Path] = ()
) -> None:
    affected = {*updates, *tracked}
    originals = {path: path.read_bytes() if path.is_file() else None for path in affected}
    try:
        for path, content in updates.items():
            _atomic_text(path, content)
        validate()
    except Exception:
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        raise


def run_generate_contract_session(
    roadmap_file: Path,
    stage_ids: Iterable[str],
    *,
    agent: SessionAgent,
    user: SessionUser,
    user_identity: dict[str, str] | None = None,
) -> Path:
    repository = _git_root(roadmap_file)
    roadmap_relative = roadmap_file.resolve().relative_to(repository).as_posix()
    selected = [stage_ids] if isinstance(stage_ids, str) else list(stage_ids)
    if not selected:
        raise PlanningError("contract generation selected no phases")
    try:
        original_binding = stage_set_binding(repository, roadmap_relative, selected)
        for phase in original_binding["stages"]:
            if phase["readiness"] != "ready":
                raise PlanningError(f"roadmap phase {phase['id']!r} is not ready")
    except RoadmapError as error:
        raise PlanningError(str(error)) from error
    response = agent.start(
        _contract_prompt(repository, roadmap_relative, selected), _CONTRACT_SCHEMA
    )
    contract_path = repository / "verification" / "contract.toml"
    while True:
        proposal_problem = ""
        user.show(str(response.get("message", "")).strip() or "No explanation was returned.")
        goals = response.get("verification_goals")
        if isinstance(goals, list):
            user.show("Verification goals:\n" + "\n".join(f"- {goal}" for goal in goals))
        try:
            updates = _validated_updates(
                repository, original_binding["specification_root"], response
            )
            roadmap_text = updates[repository / "ROADMAP.md"]
            roadmap = parse_roadmap(roadmap_text, "ROADMAP.md")
            contract_stage = _contract_value(response["contract_toml"])
            if contract_stage.get("stages") != selected:
                raise PlanningError("generated contract selects different roadmap phases")
            replacements = {
                path.relative_to(repository).as_posix(): content
                for path, content in updates.items()
                if path not in {repository / "ROADMAP.md", contract_path}
            }
            proposal_binding = proposed_stage_set_binding(
                repository,
                "ROADMAP.md",
                roadmap_text,
                selected,
                replacements,
            )
            if any(phase["readiness"] != "ready" for phase in proposal_binding["stages"]):
                raise PlanningError("every selected phase must remain ready")
            validate_interactive_trace(contract_stage, proposal_binding)
            user.show(
                "Exact approval binding:\n"
                + json.dumps(
                    {
                        "stage_ids": selected,
                        "stage_set_sha256": proposal_binding["stage_set_sha256"],
                        "global_invariants_sha256": proposal_binding["global_invariants_sha256"],
                        "semantic_closure": proposal_binding["semantic_closure"],
                        "semantic_closure_sha256": proposal_binding["semantic_closure_sha256"],
                        "semantic_trace_sha256": digest_bytes(
                            canonical_bytes(contract_stage["alignment"]["semantic_units"])
                        ),
                        "contract_sha256": digest_bytes(
                            _normalized_text(response["contract_toml"]).encode("utf-8")
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            for path, content in updates.items():
                diff = _proposal_diff(path, content)
                if diff:
                    user.show(diff)
            valid_proposal = True
        except (AlignmentError, PlanningError, RoadmapError) as error:
            roadmap = None
            contract_stage = None
            updates = {}
            valid_proposal = False
            proposal_problem = str(error)
            user.show(f"Contract proposal is not mechanically valid yet: {proposal_problem}")
        decision = user.respond(
            "Feedback, /approve these exact phase meanings, contract, and verification goals, "
            "or /quit: "
        )
        if decision == "/quit":
            raise PlanningError("contract-generation session cancelled; nothing was approved")
        if decision != "/approve":
            response = agent.respond(
                _render_agent_prompt(
                    "contract-follow-up",
                    kind="user-feedback",
                    feedback=decision,
                    problem=proposal_problem or "none",
                ),
                _CONTRACT_SCHEMA,
            )
            continue
        unresolved = response.get("unresolved")
        empty_gaps = isinstance(unresolved, dict) and all(
            unresolved.get(key) == []
            for key in (
                "ambiguities",
                "missing_acceptance_criteria",
                "unsupported_assumptions",
                "semantic_gaps",
            )
        )
        if (
            not valid_proposal
            or roadmap is None
            or contract_stage is None
            or response.get("ready_for_approval") is not True
            or response.get("contractible") is not True
            or response.get("faithful_to_sources") is not True
            or response.get("complete_specification_corpus") is not True
            or not empty_gaps
            or response.get("verification_goals")
            != contract_stage["alignment"]["verification_goals"]
        ):
            user.show("Approval denied: unresolved or mechanically invalid contract generation.")
            response = agent.respond(
                _render_agent_prompt(
                    "contract-follow-up",
                    kind="approval-denied",
                    problem=proposal_problem or "none",
                ),
                _CONTRACT_SCHEMA,
            )
            continue

        identity = user_identity or git_identity(repository)

        def qualify_written_artifacts(identity: dict[str, str] = identity) -> None:
            nonlocal contract_stage
            final_binding = stage_set_binding(repository, "ROADMAP.md", selected)
            approved = embed_contract_approval(
                contract_path.read_text(encoding="utf-8"),
                binding=final_binding,
                agent_identity=agent.identity,
                user_identity=identity,
            )
            _atomic_text(contract_path, approved)
            contract_stage = _contract_value(approved, require_approval=True)
            validate_interactive_trace(contract_stage, final_binding)

        try:
            _apply_with_rollback(
                updates,
                qualify_written_artifacts,
            )
        except (AlignmentError, ContractError, RoadmapError, PlanningError) as error:
            raise PlanningError(f"approved artifact set failed qualification: {error}") from error
        user.show(f"Approved phase contract written to {contract_path}")
        return contract_path
