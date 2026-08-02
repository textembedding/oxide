from __future__ import annotations

import codecs
import json
import os
import re
import shutil
import sys
import threading
import time
from dataclasses import dataclass
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    TextIO,
    Tuple,
)

try:
    from pygments import highlight as _pygments_highlight
    from pygments.formatters import TerminalFormatter as _TerminalFormatter
    from pygments.lexers import (
        get_lexer_by_name as _get_lexer_by_name,
        guess_lexer as _guess_lexer,
    )
    from pygments.util import ClassNotFound as _PygmentsClassNotFound
except ImportError:  # pragma: no cover - exercised by the built-in fallback
    _pygments_highlight = None
    _TerminalFormatter = None
    _get_lexer_by_name = None
    _guess_lexer = None
    _PygmentsClassNotFound = ValueError


class LiveObserverError(RuntimeError):
    pass


OBSERVER_SLOT_COUNT = 8
_SLOT_STATES = frozenset({"WAITING", "IDLE", "ACTIVE", "COMPLETE", "FAILED"})
_STREAM_KINDS = frozenset({"stdout", "stderr", "controller", "lifecycle"})
_OBSERVATION_KINDS = frozenset({"start", "chunk", "eof", "finish"})
_SMOOTH_SCROLL_FRAME_SECONDS = 1.0 / 60.0
_SMOOTH_SCROLL_MAX_BATCH_SECONDS = 0.5
_YAML_STRING_STYLE = "38;5;208"
_JSON_TOKEN = re.compile(
    r'(?P<key>"(?:\\.|[^"\\])*")(?=\s*:)'
    r'|(?P<string>"(?:\\.|[^"\\])*")'
    r"|(?P<number>-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)"
    r"|(?P<literal>\b(?:true|false|null)\b)"
)
_MARKDOWN_FENCE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})"
    r"(?P<info>[^\r\n]*)$"
)
_BASIC_CODE_TOKEN = re.compile(
    r"(?P<comment>\#[^\n]*|//[^\n]*|/\*.*?\*/)"
    r"|(?P<string>'''(?:.|\n)*?'''|\"\"\"(?:.|\n)*?\"\"\""
    r"|'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\")"
    r"|(?P<number>\b(?:0[xob][0-9A-Fa-f]+|\d+(?:\.\d+)?)\b)"
    r"|(?P<keyword>\b(?:"
    r"and|as|async|await|break|case|catch|class|const|continue|def|do|"
    r"elif|else|enum|except|export|false|finally|fn|for|from|func|"
    r"function|if|import|in|interface|let|match|mod|none|null|or|"
    r"package|pass|pub|raise|return|self|static|struct|switch|throw|"
    r"true|try|type|use|var|while|with|yield"
    r")\b)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_GIT_LOG_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<hash>[0-9a-f]{7,40})(?P<rest>(?:\s|$).*)$"
)
_GIT_STAT_LINE = re.compile(
    r"^(?P<indent>\s*)(?P<path>.+?)(?P<separator>\s+\|\s+)"
    r"(?P<count>\d+)(?P<space>\s+)(?P<bars>[+-]+)$"
)
_GIT_SUMMARY_LINE = re.compile(
    r"^\s*\d+\s+files?\s+changed"
    r"(?:,\s+\d+\s+insertions?\(\+\))?"
    r"(?:,\s+\d+\s+deletions?\(-\))?\s*$"
)
_MARKDOWN_HEADING = re.compile(
    r"^(?P<indent>\s*)(?P<marks>#{1,6})(?P<space>\s+)(?P<body>.*)$"
)
_MARKDOWN_LIST = re.compile(
    r"^(?P<indent>\s*)(?P<marker>(?:[-*+]|\d+[.)]))"
    r"(?P<space>\s+)(?P<body>.*)$"
)
_MARKDOWN_QUOTE = re.compile(
    r"^(?P<indent>\s*)(?P<marker>>+)(?P<space>\s?)(?P<body>.*)$"
)
_YAML_MAPPING_LINE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<marker>-\s+)?"
    r"(?P<key>[A-Za-z_][A-Za-z0-9_-]*|\"(?:\\.|[^\"\\])*\")"
    r"(?P<colon>:)(?P<space>[ \t]*)(?P<value>.*)$"
)
_YAML_NUMBER = re.compile(
    r"[-+]?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][-+]?\d+)?"
)
_SEMANTIC_TOKEN = re.compile(
    r"(?P<inline_code>`[^`\n]+`)"
    r"|(?P<link>\[[^\]\n]+\]\([^) \n]+(?:\s+\"[^\"]*\")?\))"
    r"|(?P<path>(?<![\w.-])(?:\.{0,2}/)?"
    r"(?:[\w@+.-]+/)+[\w@+.-]+"
    r"(?:\.(?:bash|c|cc|cpp|css|go|h|hpp|html|java|js|json|jsx|md|py|"
    r"rs|sh|sql|toml|ts|tsx|xml|yaml|yml|zsh))?(?::\d+)?"
    r"|(?<![\w.-])[\w@+.-]+\."
    r"(?:bash|c|cc|cpp|css|go|h|hpp|html|java|js|json|jsx|md|py|"
    r"rs|sh|sql|toml|ts|tsx|xml|yaml|yml|zsh)(?::\d+)?)"
    r"|(?P<hash>\b[0-9a-f]{7,40}\b)"
    r"|(?P<failure>\b(?:blocked|cancelled|deletions?|error|failed|failure|"
    r"invalid|missing|rejected|stale|timed?\s*out|timeout)\b(?:\(-\))?)"
    r"|(?P<success>\b(?:accepted|complete|completed|ok|pass|passed|"
    r"insertions?|ready|success|succeeded|valid)\b(?:\(\+\))?)"
    r"|(?P<activity>\b(?:active|assigned|claim(?:ed)?|launch(?:ed|ing)?|"
    r"pending|running|started|waiting)\b)"
    r"|(?P<number>\b\d+(?:\.\d+)?(?:ms|s|m|h|%|B|KB|MB|GB)?\b)",
    re.IGNORECASE,
)
_SOURCE_SUFFIX_LANGUAGES = {
    ".bash": "bash",
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".css": "css",
    ".go": "go",
    ".h": "c",
    ".hpp": "cpp",
    ".html": "html",
    ".java": "java",
    ".js": "javascript",
    ".json": "json",
    ".jsx": "jsx",
    ".md": "markdown",
    ".py": "python",
    ".rs": "rust",
    ".sh": "bash",
    ".sql": "sql",
    ".toml": "toml",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".xml": "xml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".zsh": "bash",
}


@dataclass(frozen=True)
class ObserverSlot:
    """One stable logical observer target.

    Slot zero always follows replaceable orchestrator invocations. Slots one
    through seven follow worker slots zero through six. A slot is deliberately
    not a process identity.
    """

    index: int
    actor: str

    @classmethod
    def parse(cls, value: object) -> "ObserverSlot":
        if isinstance(value, cls):
            return value
        if isinstance(value, bool):
            raise LiveObserverError("observer slot must not be boolean")
        if isinstance(value, int):
            for slot in observer_slots():
                if slot.index == value:
                    return slot
            raise LiveObserverError("observer slot index must be in 0..7")
        if isinstance(value, str):
            for slot in observer_slots():
                if slot.actor == value:
                    return slot
            raise LiveObserverError(
                "observer slot must be orchestrator or worker-0..worker-6"
            )
        raise LiveObserverError("observer slot must be a name or integer index")


def observer_slots() -> Tuple[ObserverSlot, ...]:
    return (ObserverSlot(0, "orchestrator"),) + tuple(
        ObserverSlot(index + 1, "worker-{}".format(index))
        for index in range(7)
    )


ObservationLoader = Callable[
    [ObserverSlot, int, int],
    Sequence[Mapping[str, Any]],
]
ObservationStateLoader = Callable[[ObserverSlot], str]


class JournalSlotRenderer:
    """Render journal observation rows for one logical slot.

    The caller owns the journal schema and supplies generic mappings. Required
    row fields are ``event_id`` and ``payload``. Optional fields are
    ``observer_slot``/``slot_index``, ``slot``, ``kind``,
    ``stream_kind``/``stream_name``,
    ``invocation_id``, ``actor_kind``, ``task_id``, ``attempt_id``, ``role``,
    ``stream_closed``, and invocation terminal metadata. Journal rows use
    ``start``, ``chunk``, ``eof``, and ``finish`` kinds. ``eof`` and
    ``finish`` close their relevant decoder state even when
    ``stream_closed`` is absent.

    Payloads may be bytes (the preferred exact journal representation) or
    strings. UTF-8 and JSONL may be split across rows. No row value reaches a
    terminal without passing through the renderer's escaping rules.
    """

    def __init__(self, slot: ObserverSlot, *, color: bool = False) -> None:
        self.slot = ObserverSlot.parse(slot.index)
        self.color = bool(color)
        self.current_task_id: Optional[str] = None
        self._last_invocation: Optional[str] = None
        self._decoders: Dict[Tuple[str, str], Any] = {}
        self._pending_json: Dict[Tuple[str, str], str] = {}

    def feed(self, row: Mapping[str, Any]) -> str:
        if not isinstance(row, Mapping):
            raise LiveObserverError("observation row must be a mapping")
        self._validate_slot(row)
        kind = row.get("kind", "chunk")
        if kind not in _OBSERVATION_KINDS:
            raise LiveObserverError(
                "observation kind must be one of {}".format(
                    ", ".join(sorted(_OBSERVATION_KINDS))
                )
            )
        stream = row.get("stream_kind", row.get("stream_name", "stdout"))
        if stream not in _STREAM_KINDS:
            raise LiveObserverError(
                "observation stream must be one of {}".format(
                    ", ".join(sorted(_STREAM_KINDS))
                )
            )
        invocation = row.get("invocation_id")
        if invocation is None:
            invocation_id = "controller"
        elif isinstance(invocation, str) and invocation:
            invocation_id = invocation
        else:
            raise LiveObserverError(
                "observation invocation_id must be a nonempty string or null"
            )
        if kind == "start":
            task_id = row.get("task_id")
            self.current_task_id = (
                str(task_id) if task_id not in (None, "") else None
            )

        boundary = ""
        if self._last_invocation != invocation_id:
            boundary = self._boundary(invocation_id, row)
            self._last_invocation = invocation_id

        if kind == "start":
            # The invocation boundary is observer context, not lifecycle log
            # content. Start metadata remains durable in the journal but is
            # intentionally silent in the human projection.
            return boundary
        if kind == "finish":
            flushed = self._flush_invocation(invocation_id)
            # Finish still closes split UTF-8/JSONL state, but its status,
            # backend, duration, usage, and error metadata are not rendered.
            return flushed
        if stream == "lifecycle":
            return ""

        key = (invocation_id, str(stream))
        payload = row.get("payload", b"")
        closed = bool(row.get("stream_closed", False)) or kind == "eof"
        text = self._decode_payload(key, payload, final=closed)
        combined = self._pending_json.pop(key, "") + text
        rendered, pending = _render_stream_chunk(
            str(stream),
            combined,
            final=closed,
            color=self.color,
        )
        if pending:
            self._pending_json[key] = pending

        return boundary + rendered

    def finish(self) -> str:
        rendered: List[str] = []
        keys = set(self._decoders) | set(self._pending_json)
        for key in sorted(keys):
            decoder = self._decoders.pop(key, None)
            tail = decoder.decode(b"", final=True) if decoder is not None else ""
            combined = self._pending_json.pop(key, "") + tail
            if combined:
                value, pending = _render_stream_chunk(
                    key[1],
                    combined,
                    final=True,
                    color=self.color,
                )
                if pending:
                    raise LiveObserverError(
                        "final observation fragment was not consumed"
                    )
                rendered.append(value)
        return "".join(rendered)

    def _flush_invocation(self, invocation_id: str) -> str:
        rendered: List[str] = []
        keys = {
            key
            for key in set(self._decoders) | set(self._pending_json)
            if key[0] == invocation_id
        }
        for key in sorted(keys):
            decoder = self._decoders.pop(key, None)
            tail = decoder.decode(b"", final=True) if decoder is not None else ""
            combined = self._pending_json.pop(key, "") + tail
            if combined:
                value, pending = _render_stream_chunk(
                    key[1],
                    combined,
                    final=True,
                    color=self.color,
                )
                if pending:
                    raise LiveObserverError(
                        "final observation fragment was not consumed"
                    )
                rendered.append(value)
        return "".join(rendered)

    def _validate_slot(self, row: Mapping[str, Any]) -> None:
        raw_index = row.get("observer_slot", row.get("slot_index"))
        if raw_index is not None:
            if (
                not isinstance(raw_index, int)
                or isinstance(raw_index, bool)
                or raw_index != self.slot.index
            ):
                raise LiveObserverError("observation row belongs to another slot")
        if "slot" in row and row["slot"] != self.slot.actor:
            raise LiveObserverError("observation row belongs to another slot")

    def _decode_payload(
        self,
        key: Tuple[str, str],
        payload: object,
        *,
        final: bool,
    ) -> str:
        if isinstance(payload, memoryview):
            payload = payload.tobytes()
        if isinstance(payload, bytearray):
            payload = bytes(payload)
        if isinstance(payload, bytes):
            decoder = self._decoders.get(key)
            if decoder is None:
                decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                self._decoders[key] = decoder
            text = decoder.decode(payload, final=final)
            if final:
                self._decoders.pop(key, None)
            return text
        if isinstance(payload, str):
            if final:
                decoder = self._decoders.pop(key, None)
                prefix = (
                    decoder.decode(b"", final=True)
                    if decoder is not None
                    else ""
                )
                return prefix + payload
            return payload
        raise LiveObserverError("observation payload must be bytes or string")

    def _boundary(
        self,
        invocation_id: str,
        row: Mapping[str, Any],
    ) -> str:
        details = [
            "invocation={}".format(_terminal_safe(invocation_id)),
            "slot={}".format(_terminal_safe(self.slot.actor)),
        ]
        for key, label in (
            ("actor_kind", "actor"),
            ("task_id", "task"),
            ("attempt_id", "attempt"),
            ("role", "role"),
        ):
            value = row.get(key)
            if value not in (None, ""):
                details.append(
                    "{}={}".format(label, _terminal_safe(str(value)))
                )
        if self.color:
            styled_details: List[str] = []
            for detail in details:
                label, value = detail.split("=", 1)
                styled_details.append(
                    "{}={}".format(
                        _style(label, "2;36", True),
                        _style(
                            value,
                            (
                                "1;33"
                                if label == "invocation"
                                else "1;36"
                                if label == "slot"
                                else "1;34"
                                if label == "task"
                                else "1;35"
                            ),
                            True,
                        ),
                    )
                )
            body = " ".join(styled_details)
            return "\n{} {} {}\n".format(
                _style("========", "2;36", True),
                body,
                _style("========", "2;36", True),
            )
        return "\n======== {} ========\n".format(" ".join(details))


class JournalSlotFollower:
    """Pure-Python replay-and-follow loop over journal observation rows."""

    def __init__(
        self,
        slot: object,
        load_rows: ObservationLoader,
        *,
        load_state: Optional[ObservationStateLoader] = None,
        output: Optional[TextIO] = None,
        after_event_id: int = 0,
        batch_size: int = 256,
        poll_interval: float = 0.1,
        color: Optional[bool] = None,
    ) -> None:
        if not callable(load_rows):
            raise TypeError("load_rows must be callable")
        if load_state is not None and not callable(load_state):
            raise TypeError("load_state must be callable")
        if (
            not isinstance(after_event_id, int)
            or isinstance(after_event_id, bool)
            or after_event_id < 0
        ):
            raise ValueError("after_event_id must be a nonnegative integer")
        if (
            not isinstance(batch_size, int)
            or isinstance(batch_size, bool)
            or batch_size < 1
        ):
            raise ValueError("batch_size must be a positive integer")
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.slot = ObserverSlot.parse(slot)
        self.load_rows = load_rows
        self.load_state = load_state
        self.output = output or sys.stdout
        isatty = getattr(self.output, "isatty", None)
        terminal_output = bool(
            callable(isatty)
            and isatty()
        )
        if color is None:
            color = bool(
                terminal_output
                and "NO_COLOR" not in os.environ
            )
        self.cursor = after_event_id
        self.batch_size = batch_size
        self.poll_interval = float(poll_interval)
        self.color = bool(color)
        self.renderer = JournalSlotRenderer(self.slot, color=self.color)
        self._started = False
        self._last_state: Optional[str] = None
        self._terminal_output = terminal_output
        self._tui_active = False
        self._tui_size: Optional[Tuple[int, int]] = None
        self._next_scroll_frame_at: Optional[float] = None

    def start(self) -> None:
        if self._started:
            return
        self._write(
            "{} {}={} {}={} {}\n".format(
                _style("========", "2;36", self.color),
                _style("observer slot", "1;36", self.color),
                _style(self.slot.actor, "1;33", self.color),
                _style("index", "1;36", self.color),
                _style(str(self.slot.index), "1;33", self.color),
                _style("========", "2;36", self.color),
            )
        )
        self._publish_state()
        self._started = True

    def poll_once(self) -> int:
        self.start()
        rows = self.load_rows(self.slot, self.cursor, self.batch_size)
        if not isinstance(rows, Sequence):
            rows = tuple(rows)
        if len(rows) > self.batch_size:
            raise LiveObserverError("observation loader exceeded its batch limit")

        count = 0
        prior = self.cursor
        rendered_rows: List[str] = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise LiveObserverError("observation row must be a mapping")
            event_id = row.get("event_id")
            if (
                not isinstance(event_id, int)
                or isinstance(event_id, bool)
                or event_id <= prior
            ):
                raise LiveObserverError(
                    "observation rows must have strictly increasing event_id"
                )
            rendered = self.renderer.feed(row)
            if rendered:
                rendered_rows.append(rendered)
            prior = event_id
            self.cursor = event_id
            count += 1
        if rendered_rows:
            self._write("".join(rendered_rows), animate=True)
        self._publish_state()
        return count

    def run(
        self,
        *,
        follow: bool = True,
        stop_event: Optional[threading.Event] = None,
    ) -> int:
        if stop_event is not None and not hasattr(stop_event, "wait"):
            raise TypeError("stop_event must provide wait()")
        try:
            if follow and self._terminal_output:
                self._start_tui()
            self.start()
            while True:
                count = self.poll_once()
                if not follow:
                    if count < self.batch_size:
                        break
                    continue
                if stop_event is not None and stop_event.is_set():
                    break
                if count:
                    continue
                if stop_event is None:
                    time.sleep(self.poll_interval)
                elif stop_event.wait(self.poll_interval):
                    break
            tail = self.renderer.finish()
            if tail:
                self._write(tail, animate=True)
            return self.cursor
        finally:
            if self._tui_active:
                self._stop_tui()

    def _publish_state(self) -> None:
        state = self.load_state(self.slot) if self.load_state else "IDLE"
        if not isinstance(state, str):
            raise LiveObserverError("observer state must be a string")
        state = state.upper()
        if state not in _SLOT_STATES:
            raise LiveObserverError(
                "observer state must be one of {}".format(
                    ", ".join(sorted(_SLOT_STATES))
                )
            )
        changed = state != self._last_state
        self._last_state = state
        if self._tui_active:
            if changed:
                self._draw_footer()
            return
        if changed:
            color = {
                "WAITING": "33",
                "IDLE": "36",
                "ACTIVE": "1;32",
                "COMPLETE": "1;32",
                "FAILED": "1;31",
            }[state]
            self._write(
                "{} {}={}\n".format(
                    _style("[observer]", "2;36", self.color),
                    _style("state", "1;36", self.color),
                    _style(state, color, self.color),
                )
            )

    def _write(self, value: str, *, animate: bool = False) -> None:
        if animate and self._tui_active:
            self._write_smoothly(value)
            return
        if self._tui_active:
            self._clear_footer()
        self.output.write(value)
        if self._tui_active:
            self._draw_footer()
        self.output.flush()

    def _write_smoothly(self, value: str) -> None:
        """Pace terminal rows on an absolute clock without delaying storage."""

        frames = value.splitlines(keepends=True)
        if not frames:
            return
        if "".join(frames) != value:
            raise LiveObserverError("smooth-scroll framing changed rendered output")
        interval = min(
            _SMOOTH_SCROLL_FRAME_SECONDS,
            _SMOOTH_SCROLL_MAX_BATCH_SECONDS / max(1, len(frames)),
        )
        now = time.monotonic()
        deadline = self._next_scroll_frame_at
        if (
            deadline is None
            or deadline < now - _SMOOTH_SCROLL_FRAME_SECONDS
            or deadline > now + _SMOOTH_SCROLL_MAX_BATCH_SECONDS
        ):
            deadline = now
        for frame in frames:
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            self._clear_footer()
            self.output.write(frame)
            self._draw_footer()
            self.output.flush()
            deadline = max(deadline + interval, time.monotonic())
        self._next_scroll_frame_at = deadline

    def _start_tui(self) -> None:
        self._tui_active = True
        self._next_scroll_frame_at = None
        # Stay on the primary screen so terminal-native scrollback remains
        # available without turning the observer into an input-driven UI.
        # DECSCLM asks supporting emulators to interpolate each paced row.
        # Unsupported emulators safely ignore it and still receive the
        # absolute-clock line animation above.
        self.output.write("\x1b[?25l\x1b[?4h")
        self._configure_tui(force=True)
        self._draw_footer()
        self.output.flush()

    def _stop_tui(self) -> None:
        self._clear_footer()
        self.output.write("\x1b[?4l\x1b[?25h")
        self.output.flush()
        self._tui_active = False
        self._tui_size = None
        self._next_scroll_frame_at = None

    def _configure_tui(self, *, force: bool = False) -> None:
        size = shutil.get_terminal_size(fallback=(80, 24))
        columns = max(20, int(size.columns))
        rows = max(3, int(size.lines))
        current = (columns, rows)
        if force or current != self._tui_size:
            if self._tui_size is not None:
                old_rows = self._tui_size[1]
                self.output.write(
                    "\x1b[s\x1b[{};1H\x1b[2K\x1b[u".format(
                        old_rows
                    )
                )
            self._tui_size = current

    def _clear_footer(self) -> None:
        if not self._tui_active:
            return
        self._configure_tui()
        assert self._tui_size is not None
        rows = self._tui_size[1]
        self.output.write(
            "\x1b[s\x1b[{};1H\x1b[2K\x1b[u".format(rows)
        )

    def _draw_footer(self) -> None:
        if not self._tui_active:
            return
        self._configure_tui()
        assert self._tui_size is not None
        columns, rows = self._tui_size
        state = self._last_state or "WAITING"
        task_id = self.renderer.current_task_id or "-"
        footer = "{}  thread={}  task_id={}".format(
            self.slot.actor,
            state,
            _terminal_safe(task_id),
        )
        footer = footer[:columns].ljust(columns)
        self.output.write(
            "\x1b[s\x1b[{};1H\x1b[2K{}\x1b[u".format(
                rows,
                # ANSI cells cannot carry alpha. Use the terminal's default
                # background so its configured window transparency survives.
                _style(footer, "2;37;49", True),
            )
        )


def _render_stream_chunk(
    stream_name: str,
    text: str,
    *,
    final: bool = False,
    color: bool = False,
) -> tuple:
    rendered: List[str] = []
    pending = ""
    # Codex stdout is JSONL: LF is the only record boundary. str.splitlines()
    # also splits valid JSON string data at U+0085/U+2028/U+2029.
    lines: List[str] = []
    offset = 0
    while True:
        newline = text.find("\n", offset)
        if newline < 0:
            if offset < len(text):
                lines.append(text[offset:])
            break
        lines.append(text[offset : newline + 1])
        offset = newline + 1
    for line in lines:
        terminated = line.endswith("\n")
        content = line[:-1] if terminated else line
        if terminated and content.endswith("\r"):
            content = content[:-1]
        if not final and not terminated and _looks_like_json_fragment(content):
            try:
                json.loads(content)
            except (TypeError, ValueError):
                pending = content
                continue
        rendered.append(_render_line(stream_name, content, color=color))
    return "".join(rendered), pending


def _looks_like_json_fragment(value: str) -> bool:
    stripped = value.lstrip()
    return stripped.startswith("{") or stripped.startswith("[")


def _render_line(stream_name: str, line: str, *, color: bool = False) -> str:
    try:
        event = json.loads(line)
    except (TypeError, ValueError):
        return _render_stream_text(
            stream_name,
            _highlight_semantic_text(
                _terminal_safe(line),
                color=color,
                base_code=(
                    "38;5;250"
                    if stream_name == "stdout"
                    else "31"
                    if stream_name == "stderr"
                    else "2;36"
                ),
            ),
            color=color,
        )
    if not isinstance(event, Mapping):
        return _render_stream_text(
            stream_name,
            _highlight_semantic_text(
                _terminal_safe(line),
                color=color,
                base_code="38;5;250",
            ),
            color=color,
        )

    known = _render_known_event(event, color=color)
    if known is not None:
        return _render_stream_text(stream_name, known, color=color)
    # Preserve unknown events visibly in terminal-escaped form so a newer
    # Codex JSONL schema remains inspectable instead of being discarded.
    event_type = _safe_multiline(str(event.get("type", "unknown")))
    return _render_stream_text(
        stream_name,
        "{} {}\n{}".format(
            _style("EVENT", "1;35", color),
            _style(event_type, "1;34", color),
            _indent_block(_pretty_value(event, color=color)),
        ),
        color=color,
    )


def _render_known_event(
    event: Mapping[str, Any],
    *,
    color: bool = False,
) -> Optional[str]:
    event_type = event.get("type")
    if event_type == "thread.started":
        return "{} {}".format(
            _style("THREAD START", "1;36", color),
            _style(
                _safe_multiline(str(event.get("thread_id", ""))),
                "1;33",
                color,
            ),
        )
    if event_type == "turn.started":
        return _style("TURN START", "1;36", color)
    if event_type == "turn.completed":
        return _style("TURN COMPLETE", "1;32", color)
    if event_type in ("turn.failed", "error"):
        detail = event.get("error", event.get("message", event))
        return "{}\n{}".format(
            _style(str(event_type).upper(), "1;31", color),
            _indent_block(_pretty_value(detail, color=color)),
        )
    if event_type in ("item.started", "item.completed", "item.updated"):
        item = event.get("item")
        if not isinstance(item, Mapping):
            return None
        phase = str(event_type).split(".", 1)[1].upper()
        return _render_item(phase, item, color=color)
    return None


def _render_item(
    phase: str,
    item: Mapping[str, Any],
    *,
    color: bool = False,
) -> str:
    item_type = str(item.get("type", "item"))
    if item_type in ("reasoning", "agent_message"):
        text = item.get("text", item.get("content", ""))
        label = "REASONING" if item_type == "reasoning" else "AGENT"
        code = "1;35" if item_type == "reasoning" else "1;32"
        return "{} [{}]\n{}".format(
            _style(label, code, color),
            _style(_safe_multiline(phase), "2", color),
            _indent_block(
                _message_value(
                    text,
                    color=color,
                    base_code=(
                        "38;5;183"
                        if item_type == "reasoning"
                        else "38;5;153"
                    ),
                )
            ),
        )
    if item_type in ("command_execution", "command"):
        command = item.get("command", item.get("cmd", ""))
        output = item.get(
            "aggregated_output",
            item.get("output", item.get("result", "")),
        )
        status = item.get("status", "")
        exit_code = item.get("exit_code")
        parts = [
            _style("COMMAND", "1;36", color),
            _style(_safe_multiline(phase), "2", color),
        ]
        if status:
            parts.append(
                _style(
                    "status={}".format(_safe_multiline(str(status))),
                    _status_color(str(status)),
                    color,
                )
            )
        if exit_code is not None:
            parts.append(
                _style(
                    "exit={}".format(_safe_multiline(str(exit_code))),
                    "33" if exit_code else "32",
                    color,
                )
            )
        rendered = [
            " ".join(parts),
            _labeled_code_block(
                "command",
                command,
                language="bash",
                color=color,
            ),
        ]
        if output:
            rendered.append(
                "{}:\n{}".format(
                    _style("output", "1;34", color),
                    _indent_block(
                        _command_output_value(
                            command,
                            output,
                            color=color,
                        )
                    ),
                )
            )
        return "\n".join(rendered)
    if item_type in ("mcp_tool_call", "tool_call"):
        server = item.get("server", "")
        tool = item.get("tool", item.get("name", ""))
        arguments = item.get("arguments", item.get("input", ""))
        result = item.get("result", item.get("output", ""))
        status = item.get("status", "")
        error = item.get("error")
        call = "{}.{}".format(server, tool).strip(".")
        parts = [
            _style("TOOL", "1;36", color),
            _style(_safe_multiline(phase), "2", color),
            _style(_safe_multiline(call), "1;34", color),
        ]
        if status:
            parts.append(
                _style(
                    "status={}".format(_safe_multiline(str(status))),
                    _status_color(str(status)),
                    color,
                )
            )
        rendered = [
            " ".join(parts),
            _tool_input_block(arguments, color=color),
        ]
        if result not in (None, "", {}, []):
            rendered.append(
                _tool_output_block(
                    result,
                    language=(
                        "yaml"
                        if server == "journal"
                        and tool in {"journal_add", "journal_search"}
                        else None
                    ),
                    color=color,
                )
            )
        if error not in (None, ""):
            rendered.append(_labeled_block("error", error, color=color))
        return "\n".join(rendered)
    if item_type == "file_change":
        status = item.get("status", "")
        parts = [
            _style("FILES", "1;36", color),
            _style(_safe_multiline(phase), "2", color),
        ]
        if status:
            parts.append(
                _style(
                    "status={}".format(_safe_multiline(str(status))),
                    _status_color(str(status)),
                    color,
                )
            )
        rendered = [" ".join(parts)]
        changes = item.get("changes", [])
        if isinstance(changes, list):
            for change in changes:
                if isinstance(change, Mapping):
                    kind = _safe_multiline(str(change.get("kind", "change")))
                    path = _safe_multiline(str(change.get("path", "")))
                    rendered.append(
                        _indent_block(
                            "{} {}".format(
                                _style(kind, "33", color),
                                _style(path, "1;36", color),
                            )
                        )
                    )
        return "\n".join(rendered)
    return "{} {} {}={}\n{}".format(
        _style("ITEM", "1;35", color),
        _style(_safe_multiline(phase), "2", color),
        _style("type", "1;36", color),
        _style(_safe_multiline(item_type), "1;34", color),
        _indent_block(_pretty_value(item, color=color)),
    )


def _message_value(
    value: Any,
    *,
    color: bool,
    base_code: str,
) -> str:
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return _markdown_value(
                value,
                color=color,
                base_code=base_code,
            )
        return _pretty_value(parsed, color=color)
    return _pretty_value(value, color=color)


def _pretty_value(value: Any, *, color: bool) -> str:
    if isinstance(value, str):
        return _highlight_semantic_text(
            _safe_multiline(value),
            color=color,
            base_code="38;5;250",
        )
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
    except (TypeError, ValueError):
        return _safe_multiline(str(value))
    rendered = "\n".join(
        _terminal_safe(line) for line in rendered.split("\n")
    )
    if not color:
        return rendered

    def highlight(match: re.Match) -> str:
        token = match.group(0)
        if match.group("key") is not None:
            return _style(token, "36", True)
        if match.group("string") is not None:
            return _style(token, "32", True)
        if match.group("number") is not None:
            return _style(token, "33", True)
        return _style(token, "35", True)

    return _JSON_TOKEN.sub(highlight, rendered)


def _labeled_block(label: str, value: Any, *, color: bool) -> str:
    return "{}:\n{}".format(
        _style(label, "1;34", color),
        _indent_block(_pretty_value(value, color=color)),
    )


def _labeled_code_block(
    label: str,
    value: Any,
    *,
    language: str,
    color: bool,
) -> str:
    if not isinstance(value, str):
        return _labeled_block(label, value, color=color)
    return "{}:\n{}".format(
        _style(label, "1;34", color),
        _indent_block(_highlight_code(value, language, color=color)),
    )


def _tool_input_block(value: Any, *, color: bool) -> str:
    if isinstance(value, Mapping) and set(value) == {"yaml"}:
        content = value["yaml"]
        if not isinstance(content, str):
            return _labeled_block("input", value, color=color)
        return "{}:\n{}".format(
            _style("input.yaml", "1;34", color),
            _indent_block(
                _highlight_code(
                    content.rstrip("\n"),
                    "yaml",
                    color=color,
                )
            ),
        )
    if isinstance(value, Mapping) and set(value) in ({"query"}, {"text"}):
        name = next(iter(value))
        content = value[name]
        if name == "text" and isinstance(content, str):
            rendered = _markdown_value(
                content,
                color=color,
                base_code="0",
            )
        else:
            rendered = _pretty_value(content, color=color)
        return "{}:\n{}".format(
            _style(
                "input.{}".format(name),
                "1;34" if name == "text" else "1;33",
                color,
            ),
            _indent_block(rendered),
        )
    return _labeled_block("input", value, color=color)


def _tool_output_block(
    value: Any,
    *,
    language: Optional[str] = None,
    color: bool,
) -> str:
    result = _tool_result_value(value)
    if isinstance(result, str) and language == "yaml":
        return "{}:\n{}".format(
            _style("output.yaml", "1;34", color),
            _indent_block(
                _highlight_code(
                    result.rstrip("\n"),
                    "yaml",
                    color=color,
                )
            ),
        )
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError):
            rendered = _markdown_value(
                result,
                color=color,
                base_code="0",
            )
        else:
            rendered = _pretty_value(parsed, color=color)
    else:
        rendered = _pretty_value(result, color=color)
    return "{}:\n{}".format(
        _style("output", "1;34", color),
        _indent_block(rendered),
    )


def _tool_result_value(value: Any) -> Any:
    if not isinstance(value, Mapping):
        return value
    content = value.get("content")
    structured = value.get(
        "structured_content",
        value.get("structuredContent"),
    )
    if (
        isinstance(content, list)
        and len(content) == 1
        and isinstance(content[0], Mapping)
        and content[0].get("type") == "text"
        and isinstance(content[0].get("text"), str)
        and structured is None
    ):
        text = content[0]["text"]
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return text
    return value


def _indent_block(value: str, prefix: str = "  ") -> str:
    return "\n".join(prefix + line for line in value.split("\n"))


def _safe_multiline(value: str) -> str:
    return "\n".join(
        _terminal_safe(line) for line in value.split("\n")
    )


def _render_stream_text(
    stream_name: str,
    value: str,
    *,
    color: bool,
) -> str:
    if stream_name == "stdout":
        return "{}\n".format(value)
    prefix = _style(
        "[{}]".format(_terminal_safe(stream_name)),
        "1;31" if stream_name == "stderr" else "2",
        color,
    )
    return "".join(
        "{} {}\n".format(prefix, line)
        for line in value.split("\n")
    )


def _markdown_value(
    value: str,
    *,
    color: bool,
    base_code: str,
) -> str:
    safe = _safe_multiline(value)
    if not color:
        return safe
    lines = safe.split("\n")
    rendered: List[str] = []
    code_lines: List[str] = []
    fence_character: Optional[str] = None
    fence_length = 0
    language = ""

    for line in lines:
        match = _MARKDOWN_FENCE.match(line)
        if fence_character is None:
            if match is None:
                rendered.append(
                    _highlight_markdown_line(
                        line,
                        base_code=base_code,
                    )
                )
                continue
            fence = match.group("fence")
            fence_character = fence[0]
            fence_length = len(fence)
            language = match.group("info").strip().split(None, 1)[0]
            rendered.append(_style(line, "2", True))
            continue

        if (
            match is not None
            and match.group("fence")[0] == fence_character
            and len(match.group("fence")) >= fence_length
            and not match.group("info").strip()
        ):
            rendered.extend(
                _highlight_code(
                    "\n".join(code_lines),
                    language,
                    color=True,
                ).split("\n")
            )
            rendered.append(_style(line, "2", True))
            code_lines = []
            fence_character = None
            fence_length = 0
            language = ""
            continue
        code_lines.append(line)

    if fence_character is not None:
        rendered.extend(
            _highlight_code(
                "\n".join(code_lines),
                language,
                color=True,
            ).split("\n")
        )
    return "\n".join(rendered)


def _highlight_markdown_line(line: str, *, base_code: str) -> str:
    heading = _MARKDOWN_HEADING.match(line)
    if heading is not None:
        return "{}{}{}{}".format(
            heading.group("indent"),
            _style(heading.group("marks"), "2;36", True),
            heading.group("space"),
            _highlight_semantic_text(
                heading.group("body"),
                color=True,
                base_code="1;36",
            ),
        )

    listing = _MARKDOWN_LIST.match(line)
    if listing is not None:
        return "{}{}{}{}".format(
            listing.group("indent"),
            _style(listing.group("marker"), "1;36", True),
            listing.group("space"),
            _highlight_semantic_text(
                listing.group("body"),
                color=True,
                base_code=base_code,
            ),
        )

    quote = _MARKDOWN_QUOTE.match(line)
    if quote is not None:
        return "{}{}{}{}".format(
            quote.group("indent"),
            _style(quote.group("marker"), "1;35", True),
            quote.group("space"),
            _highlight_semantic_text(
                quote.group("body"),
                color=True,
                base_code="2;35",
            ),
        )

    if re.match(r"^\s*(?:---+|\*\*\*+|___+)\s*$", line):
        return _style(line, "2;36", True)
    if line.startswith(("diff ", "index ", "@@", "+++", "---", "+", "-")):
        return _highlight_diff_line(line)
    if re.match(r"^\s*\$\s+", line):
        prompt, command = line.split("$", 1)
        return "{}{}{}".format(
            prompt,
            _style("$", "1;32", True),
            _highlight_code(command, "bash", color=True),
        )
    if re.match(r"^\s*[^:]{1,48}:\s*$", line):
        return _style(line, "1;34", True)
    return _highlight_semantic_text(
        line,
        color=True,
        base_code=base_code,
    )


def _highlight_semantic_text(
    value: str,
    *,
    color: bool,
    base_code: str,
) -> str:
    if not color or not value:
        return value

    def render_line(line: str) -> str:
        rendered: List[str] = []
        offset = 0
        for match in _SEMANTIC_TOKEN.finditer(line):
            if match.start() > offset:
                rendered.append(
                    _style(line[offset : match.start()], base_code, True)
                )
            token = match.group(0)
            if match.group("inline_code") is not None:
                code = "1;33"
            elif match.group("link") is not None:
                code = "4;34"
            elif match.group("path") is not None:
                code = "1;36"
            elif match.group("hash") is not None:
                code = "1;33"
            elif match.group("failure") is not None:
                code = "1;31"
            elif match.group("success") is not None:
                code = "1;32"
            elif match.group("activity") is not None:
                code = "1;35"
            else:
                code = "33"
            rendered.append(_style(token, code, True))
            offset = match.end()
        if offset < len(line):
            rendered.append(_style(line[offset:], base_code, True))
        if not rendered and line:
            return _style(line, base_code, True)
        return "".join(rendered)

    return "\n".join(render_line(line) for line in value.split("\n"))


def _command_output_value(
    command: Any,
    value: Any,
    *,
    color: bool,
) -> str:
    if not isinstance(value, str):
        return _pretty_value(value, color=color)
    safe = _safe_multiline(value.rstrip("\n"))
    if not color:
        return safe
    language = _infer_command_output_language(command)
    rendered: List[str] = []
    pending: List[str] = []

    def flush_pending() -> None:
        if not pending:
            return
        value = "\n".join(pending)
        highlighted = (
            _highlight_code(value, language, color=True)
            if language
            else _highlight_semantic_text(
                value,
                color=True,
                base_code="38;5;250",
            )
        )
        rendered.extend(highlighted.split("\n"))
        pending.clear()

    for line in safe.split("\n"):
        git_line = _highlight_git_line(line)
        if git_line is None:
            pending.append(line)
            continue
        flush_pending()
        rendered.append(git_line)
    flush_pending()
    return "\n".join(rendered)


def _infer_command_output_language(command: Any) -> str:
    if not isinstance(command, str):
        return ""
    lowered = command.lower()
    if "git diff" in lowered and not any(
        marker in lowered for marker in ("sed ", "cat ", "head ", "tail ")
    ):
        return "diff"
    counts: Dict[str, int] = {}
    for suffix, language in _SOURCE_SUFFIX_LANGUAGES.items():
        count = len(re.findall(re.escape(suffix) + r"(?:\b|['\"])", lowered))
        if count:
            counts[language] = counts.get(language, 0) + count
    if counts:
        return max(counts, key=lambda item: counts[item])
    if re.search(r"(?:^|[\s/])(?:jq|python\d*|node|ruby)(?:\s|$)", lowered):
        if "jq" in lowered:
            return "json"
        if re.search(r"(?:^|[\s/])python\d*(?:\s|$)", lowered):
            return "python"
    return ""


def _highlight_code(value: str, language: str, *, color: bool) -> str:
    safe = _safe_multiline(value)
    if not color or not safe:
        return safe
    normalized = language.strip().lower()
    if normalized in {"diff", "patch"}:
        return "\n".join(_highlight_diff_line(line) for line in safe.split("\n"))
    if normalized in {"yaml", "yml"}:
        return _highlight_yaml(safe)

    if (
        _pygments_highlight is not None
        and _TerminalFormatter is not None
        and _get_lexer_by_name is not None
        and _guess_lexer is not None
    ):
        try:
            lexer = (
                _get_lexer_by_name(normalized)
                if normalized
                else _guess_lexer(safe)
            )
            highlighted = _pygments_highlight(
                safe,
                lexer,
                _TerminalFormatter(),
            )
            if highlighted.endswith("\n"):
                highlighted = highlighted[:-1]
            if "\x1b[" in highlighted:
                return highlighted
        except _PygmentsClassNotFound:
            pass

    def replace(match: re.Match) -> str:
        token = match.group(0)
        if match.group("comment") is not None:
            return _style(token, "2;37", True)
        if match.group("string") is not None:
            return _style(token, "33", True)
        if match.group("number") is not None:
            return _style(token, "36", True)
        return _style(token, "35", True)

    return _BASIC_CODE_TOKEN.sub(replace, safe)


def _highlight_yaml(value: str) -> str:
    rendered: List[str] = []
    lines = value.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        match = _YAML_MAPPING_LINE.fullmatch(line)
        if match is None:
            stripped = line.lstrip(" ")
            prefix = line[: len(line) - len(stripped)]
            if stripped.startswith("-"):
                scalar = stripped[1:].lstrip(" ")
                spacing = stripped[1 : len(stripped) - len(scalar)]
                rendered.append(
                    "{}{}{}{}".format(
                        prefix,
                        _style("-", "1;34", True),
                        spacing,
                        _highlight_yaml_scalar(scalar),
                    )
                )
            elif stripped.startswith("#"):
                rendered.append(prefix + _style(stripped, "2;37", True))
            else:
                rendered.append(
                    _style(line, "38;5;250", True) if line else ""
                )
            index += 1
            continue

        marker = match.group("marker") or ""
        scalar = match.group("value")
        rendered.append(
            "{}{}{}{}{}{}".format(
                match.group("indent"),
                _style(marker.rstrip(), "1;34", True) + (
                    " " if marker else ""
                ),
                _style(match.group("key"), "1;36", True),
                _style(match.group("colon"), "2;34", True),
                match.group("space"),
                _highlight_yaml_scalar(scalar),
            )
        )
        if scalar in {"|", "|-", "|+"}:
            parent_indent = len(match.group("indent"))
            block_lines: List[str] = []
            index += 1
            while index < len(lines):
                block_line = lines[index]
                indentation = len(block_line) - len(
                    block_line.lstrip(" ")
                )
                if block_line.strip() and indentation <= parent_indent:
                    break
                block_lines.append(block_line)
                index += 1
            nonempty_indents = [
                len(block_line) - len(block_line.lstrip(" "))
                for block_line in block_lines
                if block_line.strip()
            ]
            content_indent = (
                min(nonempty_indents)
                if nonempty_indents
                else parent_indent + 2
            )
            markdown = "\n".join(
                (
                    block_line[content_indent:]
                    if block_line.strip()
                    else ""
                )
                for block_line in block_lines
            )
            if _looks_like_yaml_mapping_document(markdown):
                highlighted = _highlight_yaml(markdown)
            else:
                highlighted = _markdown_value(
                    markdown,
                    color=True,
                    base_code="38;5;250",
                )
            rendered.extend(
                (
                    (" " * content_indent) + highlighted_line
                    if highlighted_line
                    else ""
                )
                for highlighted_line in highlighted.split("\n")
            )
            continue
        index += 1

    return "\n".join(rendered)


def _looks_like_yaml_mapping_document(value: str) -> bool:
    """Recognize the mapping documents carried inside journal text scalars."""

    for line in value.split("\n"):
        if line.strip():
            return _YAML_MAPPING_LINE.fullmatch(line) is not None
    return False


def _highlight_yaml_scalar(value: str) -> str:
    if not value:
        return ""
    if value in {"|", "|-", "|+", "[]", "{}"}:
        return _style(value, "1;34", True)
    if value.lower() in {"true", "false", "null", "~"}:
        return _style(value, "1;34", True)
    if _YAML_NUMBER.fullmatch(value):
        return _style(value, "1;33", True)
    if (
        len(value) >= 2
        and value[0] == value[-1]
        and value[0] in {"'", '"'}
    ):
        return _style(value, _YAML_STRING_STYLE, True)
    return _style(value, _YAML_STRING_STYLE, True)


def _highlight_diff_line(line: str) -> str:
    if line.startswith("+++") or line.startswith("---"):
        return _style(line, "1;36", True)
    if line.startswith("+"):
        return _style(line, "32", True)
    if line.startswith("-"):
        return _style(line, "31", True)
    if line.startswith("@@"):
        return _style(line, "36", True)
    if line.startswith(("diff ", "index ")):
        return _style(line, "1;34", True)
    return _style(line, "38;5;250", True)


def _highlight_git_line(line: str) -> Optional[str]:
    match = _GIT_LOG_LINE.match(line)
    if match is not None:
        rest = _highlight_semantic_text(
            match.group("rest"),
            color=True,
            base_code="38;5;250",
        )
        return "{}{}{}".format(
            match.group("indent"),
            _style(match.group("hash"), "1;33", True),
            rest,
        )

    match = _GIT_STAT_LINE.match(line)
    if match is not None:
        bars = re.sub(
            r"\++|-+",
            lambda run: _style(
                run.group(0),
                "32" if run.group(0).startswith("+") else "31",
                True,
            ),
            match.group("bars"),
        )
        return "{}{}{}{}{}{}".format(
            match.group("indent"),
            _style(match.group("path"), "36", True),
            match.group("separator"),
            _style(match.group("count"), "33", True),
            match.group("space"),
            bars,
        )

    if _GIT_SUMMARY_LINE.match(line):
        return _highlight_semantic_text(
            line,
            color=True,
            base_code="38;5;250",
        )
    if re.match(r"^\s*(?:\./)?[A-Za-z0-9_.@/+:-]+\.[A-Za-z0-9]+\s*$", line):
        return _style(line, "36", True)
    return None


def _status_color(status: str) -> str:
    normalized = status.lower()
    if normalized in {"completed", "succeeded", "success"}:
        return "32"
    if normalized in {"failed", "error", "cancelled"}:
        return "31"
    return "33"


def _style(value: str, code: str, enabled: bool) -> str:
    if not enabled:
        return value
    return "\x1b[{}m{}\x1b[0m".format(code, value)


def _terminal_safe(value: str) -> str:
    safe: List[str] = []
    for character in value:
        codepoint = ord(character)
        if codepoint == 0x0A:
            safe.append("\\n")
        elif codepoint == 0x0D:
            safe.append("\\r")
        elif codepoint == 0x09:
            safe.append("\\t")
        elif codepoint in (0x2028, 0x2029):
            safe.append("\\u{:04x}".format(codepoint))
        elif 0xD800 <= codepoint <= 0xDFFF:
            safe.append("\\u{:04x}".format(codepoint))
        elif codepoint <= 0x1F or 0x7F <= codepoint <= 0x9F:
            safe.append("\\x{:02x}".format(codepoint))
        else:
            safe.append(character)
    return "".join(safe)
