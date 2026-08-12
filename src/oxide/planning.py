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
    write_interactive_alignment_receipts,
)
from .contract import ContractError, validate_contract
from .roadmap import (
    RoadmapError,
    canonical_bytes,
    digest_bytes,
    load_roadmap,
    parse_roadmap,
    proposed_stage_binding,
    render_roadmap_document,
    roadmap_maintenance_impact,
    specification_corpus,
    validate_roadmap_approval,
    write_roadmap_approval,
)
from .verification_policy import (
    POLICY_PROFILE,
    verification_policy_digest,
    verification_policy_prompt,
)


class PlanningError(RuntimeError):
    pass


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


_PLAN_SCHEMA: dict[str, Any] = {
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


def _plan_prompt(
    repository: Path,
    specification_directory: str,
    *,
    maintenance_stage_ids: Iterable[str] = (),
    maintenance_request: str = "",
) -> str:
    corpus = specification_corpus(repository, specification_directory)
    existing = [path for path in ("ROADMAP.md", "docs/ROADMAP.md") if (repository / path).is_file()]
    bundle = _frozen_source_bundle(
        repository,
        [*(item["path"] for item in corpus), *existing],
    )
    policy = verification_policy_prompt()
    maintenance_ids = list(maintenance_stage_ids)
    maintenance = ""
    if maintenance_ids:
        maintenance = f"""
MAINTENANCE MODE
The existing approved ROADMAP.md is the immutable baseline for this session. Apply only this
user-requested change to the explicitly selected phases {json.dumps(maintenance_ids)}:
{maintenance_request}

Preserve the exact phase order and IDs, top-level roadmap fields, global invariants, and every
unselected phase. Do not add, remove, rename, reorder, or opportunistically rewrite phases. If
the request needs product semantics absent from the specifications, report the gap instead of
inventing it. Return the complete schema with only the requested phase changes.
"""
    return f"""You are Oxide's interactive planning agent. The complete frozen text of every
Markdown file under {specification_directory!r}, plus any existing roadmap, is included below.
The exact specification corpus is {json.dumps(corpus, sort_keys=True)}. Read the supplied source
bundle directly. Do not use shell, file, Git, or network tools to reread it during this turn.

Propose or refine repository-root ROADMAP.md. Specifications normally describe capabilities,
constraints, and intended behavior rather than implementation stages. Derive as many or as few
stages as the work justifies from dependency order, coherent capability increments, verification
readiness, and empirical gates. Preserve an explicit source-defined boundary when one genuinely
exists, but never assume a particular stage count, numbering scheme, naming convention, product
domain, or maturity model.

Represent the complete source-defined horizon, not only the work that is contractible today.
Trace every material current requirement, intended future capability, deferral, open research
direction, and permanent non-goal to a global invariant, a stage's included scope, or an explicit
exclusion. A future capability that lacks enough detail for contract generation belongs in a
planned, deferred, or blocked stage; do not omit it and do not invent the missing behavior or
success criteria. Top-level status='ready' means the staged plan is complete and approved as a
plan, not that every stage is ready for contract generation. Only a stage whose semantic closure
is sufficiently precise and dependency-closed may use readiness='ready'. Explain outcomes,
priorities, scope, dependencies, deferrals, and any information needed to make later work ready.
Surface ambiguities that prevent honest planning instead of silently resolving them.
Before returning, perform a second coverage pass over every source heading—especially sections
describing deferred work, future direction, open questions, non-goals, evolution, capacity, or
research—and confirm that each material item has an explicit roadmap disposition.

Oxide's normative verification policy is supplied separately below. It is mandatory for every
target and has profile {POLICY_PROFILE!r} and digest {verification_policy_digest()!r}. Apply it
to stage decomposition and every verification goal even when the target specifications contain
no abstract verification guidance. Universal assurance invariants MAY appear in the roadmap's
human-readable verification plan with sources=[], because their authority is the separately
bound Oxide policy rather than target prose. Do not cite the policy as a target product
requirement, copy it into the specification corpus, or use it to invent program behavior. The roadmap must plan
meaningful contracts, component refinement, coverage, and composition alongside implementation;
must not create an unverified production-logic category or proof-cleanup phase; and must keep
formal correctness distinct from empirical capacity.
{maintenance}

Return the complete machine-readable roadmap payload on every turn. It must contain the marker
<!-- oxide-roadmap-schema:1 --> followed by one fenced TOML block with schema=1, title,
status, specification_root, [[global_invariants]] and [[stages]]. Oxide will replace any prose
outside that block with its own standardized, human-readable projection; do not create a second
prose representation of the plan. Each invariant has id, statement, and source records with
path/anchor/requirement. Each stage must contain exactly id, outcome, included_scope,
excluded_scope, dependencies, source_specifications, applicable_global_invariants,
implementation_goals, verification_goals, and readiness; do not add fields such as priority.
Readiness is a lifecycle enum and must be exactly one of 'planned', 'ready', 'deferred', or
'blocked', never explanatory prose. Put acceptance and exit criteria in verification_goals.
Every source record must quote exact approved text under exactly one Markdown heading. A global
invariant may have sources=[] only when it states a universal requirement imposed by the supplied
Oxide verification policy; product-behavior invariants must remain exactly source-traced.
Set status='ready' and ready_for_approval=true only when the complete corpus is represented,
the roadmap is faithful, and unresolved is empty. Do not edit files yourself.

FROZEN SOURCE BUNDLE
{bundle}

{policy}
"""


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
            for stage_id in maintenance_ids:
                validate_roadmap_approval(repository, "ROADMAP.md", stage_id)
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
                    "Your proposed ROADMAP.md failed mechanical schema validation. "
                    f"Exact error: {roadmap_problem}. Repair only the Markdown/TOML structure; "
                    "preserve all source-derived product semantics and return the complete "
                    "ROADMAP.md again. Do not ask the user to resolve your formatting error.",
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
                    "Your proposed ROADMAP.md failed deterministic source-trace or maintenance "
                    f"qualification. Exact error: {roadmap_problem}. Repair the cited source "
                    "anchor and exact requirement, dependency, or scoped-maintenance change "
                    "using only the frozen source bundle and approved baseline. Preserve all "
                    "source-derived product semantics and return the complete ROADMAP.md again. "
                    "Do not ask the user to resolve a qualification error that the supplied "
                    "sources can resolve.",
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
                "You omitted the required complete ROADMAP.md. Return the complete mechanically "
                "valid artifact without changing source-derived product semantics.",
                _PLAN_SCHEMA,
            )
            continue
        mechanical_repairs = 0
        decision = user.respond("Feedback, /approve this exact roadmap, or /quit: ")
        if decision == "/quit":
            raise PlanningError("planning session cancelled; no roadmap was approved")
        if decision != "/approve":
            response = agent.respond(
                "User feedback:\n"
                + decision
                + "\nKnown mechanical issue:\n"
                + (roadmap_problem or "none")
                + "\nRevise the full proposal.",
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
                "The user attempted approval, but the proposal is not contractible. "
                f"Mechanical issue: {roadmap_problem or 'none'}. Resolve all reported gaps "
                "without silently inventing semantics.",
                _PLAN_SCHEMA,
            )
            continue
        identity = user_identity or git_identity(repository)
        approved_stage_ids = (
            maintenance_ids if maintenance_ids else [stage["id"] for stage in roadmap["stages"]]
        )
        invalidated_stage_ids = (
            maintenance_impact["invalidated_stage_ids"] if maintenance_impact is not None else []
        )

        def approve_written_roadmap(
            identity: dict[str, str] = identity,
            approved_stage_ids: tuple[str, ...] = tuple(approved_stage_ids),
            invalidated_stage_ids: tuple[str, ...] = tuple(invalidated_stage_ids),
        ) -> None:
            write_roadmap_approval(
                repository,
                "ROADMAP.md",
                specification_directory=directory,
                stage_ids=approved_stage_ids,
                agent={
                    "identity": agent.identity,
                    "complete_specification_corpus": True,
                    "faithful_to_specifications": True,
                    "unresolved": [],
                },
                user_identity=identity,
                invalidated_stage_ids=invalidated_stage_ids,
            )

        try:
            _apply_with_rollback(
                {roadmap_path: roadmap_text},
                approve_written_roadmap,
                tracked=[repository / "verification" / "roadmap-approval.json"],
            )
        except RoadmapError as error:
            raise PlanningError(f"approved roadmap failed qualification: {error}") from error
        user.show(f"Approved roadmap written to {roadmap_path}")
        return roadmap_path


def _contract_prompt(repository: Path, roadmap_path: str, stage_id: str) -> str:
    approval = validate_roadmap_approval(repository, roadmap_path, stage_id)
    binding = approval["binding"]
    source_paths = sorted({item["path"] for item in binding["semantic_closure"]})
    bundle = _frozen_source_bundle(repository, [roadmap_path, *source_paths])
    policy = verification_policy_prompt()
    return f"""You are Oxide's interactive contract-generation agent. Generate exactly one
implementation contract for roadmap stage {stage_id!r} in {roadmap_path!r}. The approved roadmap
and full text of every source specification in the selected-stage semantic closure are included
below. Read the supplied source bundle directly. Do not use shell, file, Git, or network tools to
reread it during this turn. The selected-stage semantic closure is:
{json.dumps(binding["semantic_closure"], indent=2, sort_keys=True)}

Propose coherent executable tasks, dependencies, formal Verus proof goals, supplementary
acceptance checks, and one qualified evidence slot per declared check. Explain verification
goals to the user. Surface ambiguity, missing acceptance criteria, unsupported assumptions,
or semantic gaps; never infer through them. If clarification is needed, propose complete
replacement content for each affected specification and an updated complete ROADMAP.md.
For roadmap_markdown, return the authoritative marker and TOML schema; Oxide regenerates the
standardized human-readable roadmap view from that data. Do not edit files yourself.

Return a complete schema=4 verification/contract.toml. Its stage must equal {stage_id!r}; its
goal must exactly equal the roadmap outcome. [alignment] must name ROADMAP.md, {stage_id!r},
verification/roadmap-approval.json, verification/contract-attestation.json,
verification/contract-approval.json, and verification/contract-qualification.json. It must
copy the stage implementation_goals and verification_goals exactly. Every goal, task, and
check source must contain specification, heading anchor, and exact requirement text from the
approved closure. Its specifications list must equal the closure's distinct paths. All these
files, ROADMAP.md, the cited specifications, and the contract itself must be immutable_paths.
Use Oxide's existing formal-check conventions and target verification files. Mechanical
dependencies and evidence bindings may enforce approved semantics but may not add product
behavior. Set ready_for_approval and contractible only with empty unresolved fields and a
faithful exact trace.

Oxide's normative verification policy is supplied separately below. It is a mandatory judge
input with profile {POLICY_PROFILE!r} and digest {verification_policy_digest()!r}. The contract
must set verification_policy_sha256 to exactly that digest and must operationalize the policy
through meaningful component proofs, complete production classification and coverage, trusted-
boundary declarations, deterministic integrity checks, and exact prospective-tree composition.
Those are Oxide assurance constraints and need not be misrepresented as target product citations.
Every claim about program behavior or success must still trace exactly to the approved target
semantic closure. Never use the policy to invent missing product semantics.

FROZEN SOURCE BUNDLE
{bundle}

{policy}
"""


def _contract_value(text: str) -> dict[str, Any]:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        raise PlanningError(f"generated contract TOML is invalid: {error}") from error
    try:
        return validate_contract(raw)
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
    stage_id: str,
    *,
    agent: SessionAgent,
    user: SessionUser,
    user_identity: dict[str, str] | None = None,
) -> Path:
    repository = _git_root(roadmap_file)
    roadmap_relative = roadmap_file.resolve().relative_to(repository).as_posix()
    try:
        planning_approval = validate_roadmap_approval(repository, roadmap_relative, stage_id)
    except RoadmapError as error:
        raise PlanningError(str(error)) from error
    original_binding = planning_approval["binding"]
    if original_binding["stage"]["readiness"] != "ready":
        raise PlanningError(
            f"roadmap stage {stage_id!r} is approved but not ready for contract generation"
        )
    response = agent.start(
        _contract_prompt(repository, roadmap_relative, stage_id), _CONTRACT_SCHEMA
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
            if contract_stage["stage"] != stage_id:
                raise PlanningError("generated contract selects a different roadmap stage")
            replacements = {
                path.relative_to(repository).as_posix(): content
                for path, content in updates.items()
                if path not in {repository / "ROADMAP.md", contract_path}
            }
            proposal_binding = proposed_stage_binding(
                repository,
                "ROADMAP.md",
                roadmap_text,
                stage_id,
                replacements,
            )
            validate_interactive_trace(contract_stage, proposal_binding)
            user.show(
                "Exact approval binding:\n"
                + json.dumps(
                    {
                        "stage_id": stage_id,
                        "stage_sha256": proposal_binding["stage_sha256"],
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
            "Feedback, /approve this exact stage meaning, contract, and verification goals, "
            "or /quit: "
        )
        if decision == "/quit":
            raise PlanningError("contract-generation session cancelled; nothing was approved")
        if decision != "/approve":
            response = agent.respond(
                "User feedback:\n"
                + decision
                + "\nKnown mechanical issue:\n"
                + (proposal_problem or "none")
                + "\nRevise every affected complete artifact.",
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
                "The user attempted approval, but the exact artifact set is not contractible. "
                f"Mechanical issue: {proposal_problem or 'none'}. Resolve the gaps without "
                "inventing semantics.",
                _CONTRACT_SCHEMA,
            )
            continue

        identity = user_identity or git_identity(repository)

        def qualify_written_artifacts(identity: dict[str, str] = identity) -> None:
            nonlocal contract_stage
            final_roadmap = load_roadmap(repository / "ROADMAP.md")
            selected = next(
                (stage for stage in final_roadmap["stages"] if stage["id"] == stage_id), None
            )
            if selected is None or selected["readiness"] != "ready":
                raise PlanningError("selected roadmap stage is not ready for contract generation")
            write_roadmap_approval(
                repository,
                "ROADMAP.md",
                specification_directory=final_roadmap["specification_root"],
                stage_ids=[stage_id],
                agent={
                    "identity": agent.identity,
                    "complete_specification_corpus": True,
                    "faithful_to_specifications": True,
                    "unresolved": [],
                },
                user_identity=identity,
            )
            contract_stage = _contract_value(contract_path.read_text(encoding="utf-8"))
            write_interactive_alignment_receipts(
                repository,
                contract_path,
                contract_stage,
                agent_identity=agent.identity,
                user_identity=identity,
            )

        try:
            _apply_with_rollback(
                updates,
                qualify_written_artifacts,
                tracked=[
                    repository / "verification" / "roadmap-approval.json",
                    repository / "verification" / "contract-attestation.json",
                    repository / "verification" / "contract-approval.json",
                    repository / "verification" / "contract-qualification.json",
                ],
            )
        except (AlignmentError, ContractError, RoadmapError, PlanningError) as error:
            raise PlanningError(f"approved artifact set failed qualification: {error}") from error
        user.show(f"Approved stage contract written to {contract_path}")
        return contract_path
