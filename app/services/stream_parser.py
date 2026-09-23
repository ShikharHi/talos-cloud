"""
Talos Cloud & Backend — Incremental SSE Parser & Stream Event Protocol.

Provides:
1. IncrementalSSEParser: A stateful, robust parser accepting arbitrary byte or string chunks,
   handling fragmented UTF-8, CRLF / LF line endings, multiline data: fields, comments (: heartbeat),
   and yielding complete parsed SSE event dicts {"event": str, "data": str, "id": str, "retry": int}.
2. TalosStreamEvent: Standardized protocol dataclass for Talos native stream events:
   - stream.start
   - stream.delta
   - stream.reasoning
   - stream.tool_call
   - stream.tool_result
   - stream.usage
   - stream.error
   - stream.completed
   - stream.cancelled
   - comment (: heartbeat)
"""

from __future__ import annotations

import codecs
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterator, Optional


class StreamEventType(str, Enum):
    START = "stream.start"
    DELTA = "stream.delta"
    REASONING = "stream.reasoning"
    TOOL_CALL = "stream.tool_call"
    TOOL_RESULT = "stream.tool_result"
    USAGE = "stream.usage"
    ERROR = "stream.error"
    COMPLETED = "stream.completed"
    CANCELLED = "stream.cancelled"


@dataclass
class TalosStreamEvent:
    type: str  # StreamEventType value or custom string
    stream_id: Optional[str] = None
    delta: Optional[str] = None
    reasoning: Optional[str] = None
    tool_call: Optional[dict[str, Any]] = None
    tool_result: Optional[dict[str, Any]] = None
    usage: Optional[dict[str, Any]] = None
    finish_reason: Optional[str] = None
    error: Optional[dict[str, Any]] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # Drop None values to keep wire payloads clean
        return {k: v for k, v in data.items() if v is not None}

    def to_sse_bytes(self, event_id: Optional[int | str] = None) -> bytes:
        payload = json.dumps(self.to_dict(), separators=(",", ":"))
        lines = []
        if event_id is not None:
            lines.append(f"id: {event_id}")
        lines.append(f"event: {self.type}")
        lines.append(f"data: {payload}")
        return ("\n".join(lines) + "\n\n").encode("utf-8")


@dataclass
class ParsedSSEMessage:
    event: str = "message"
    data: str = ""
    id: Optional[str] = None
    retry: Optional[int] = None
    is_comment: bool = False
    comment: str = ""


class IncrementalSSEParser:
    """
    Production-grade incremental Server-Sent Events parser.
    Maintains persistent internal buffer across arbitrary chunk splits.
    Handles:
      - UTF-8 partial byte sequences (via IncrementalDecoder)
      - CRLF (\\r\\n) and LF (\\n) line endings
      - Field continuation and multi-line `data:` fields
      - Leading space stripping per SSE specification
      - SSE comments (: heartbeat)
    """

    def __init__(self) -> None:
        self._utf8_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._text_buffer = ""
        # Current message accumulator
        self._cur_event = "message"
        self._cur_data_lines: list[str] = []
        self._cur_id: Optional[str] = None
        self._cur_retry: Optional[int] = None

    def feed(self, chunk: bytes | str) -> Iterator[ParsedSSEMessage]:
        if isinstance(chunk, bytes):
            text = self._utf8_decoder.decode(chunk, final=False)
        else:
            text = chunk

        if not text:
            return

        self._text_buffer += text

        # Parse lines while preserving partial line at the end
        start_idx = 0
        buf_len = len(self._text_buffer)
        i = 0
        while i < buf_len:
            ch = self._text_buffer[i]
            if ch == "\r":
                # Check for CRLF or lone CR
                if i + 1 < buf_len and self._text_buffer[i + 1] == "\n":
                    line = self._text_buffer[start_idx:i]
                    i += 2
                    start_idx = i
                    msg = self._process_line(line)
                    if msg is not None:
                        yield msg
                    continue
                elif i + 1 < buf_len:
                    # Lone CR
                    line = self._text_buffer[start_idx:i]
                    i += 1
                    start_idx = i
                    msg = self._process_line(line)
                    if msg is not None:
                        yield msg
                    continue
                else:
                    # Trailing \r at the very end of buffer; wait for next chunk
                    break
            elif ch == "\n":
                line = self._text_buffer[start_idx:i]
                i += 1
                start_idx = i
                msg = self._process_line(line)
                if msg is not None:
                    yield msg
                continue
            else:
                i += 1

        if start_idx > 0:
            self._text_buffer = self._text_buffer[start_idx:]

    def _process_line(self, line: str) -> Optional[ParsedSSEMessage]:
        # Empty line dispatches current event
        if not line:
            if self._cur_data_lines or self._cur_id is not None or self._cur_event != "message":
                event_data = "\n".join(self._cur_data_lines)
                msg = ParsedSSEMessage(
                    event=self._cur_event,
                    data=event_data,
                    id=self._cur_id,
                    retry=self._cur_retry,
                )
                self._cur_event = "message"
                self._cur_data_lines = []
                # id persists per SSE spec until overridden, but for event emission we include it
                return msg
            return None

        # Comment line
        if line.startswith(":"):
            comment_text = line[1:].lstrip()
            return ParsedSSEMessage(is_comment=True, comment=comment_text)

        # Field parsing
        colon_pos = line.find(":")
        if colon_pos == -1:
            field_name = line
            value = ""
        else:
            field_name = line[:colon_pos]
            value = line[colon_pos + 1 :]
            if value.startswith(" "):
                value = value[1:]

        if field_name == "data":
            self._cur_data_lines.append(value)
        elif field_name == "event":
            self._cur_event = value
        elif field_name == "id":
            if "\0" not in value:
                self._cur_id = value
        elif field_name == "retry":
            try:
                self._cur_retry = int(value)
            except ValueError:
                pass

        return None

    def flush(self) -> Iterator[ParsedSSEMessage]:
        """Flushes any remaining buffered text when stream closes."""
        remaining = self._utf8_decoder.decode(b"", final=True)
        self._text_buffer += remaining
        if self._text_buffer:
            lines = self._text_buffer.splitlines()
            self._text_buffer = ""
            for line in lines:
                msg = self._process_line(line)
                if msg is not None:
                    yield msg
            # Check final pending event
            if self._cur_data_lines or self._cur_event != "message":
                msg = ParsedSSEMessage(
                    event=self._cur_event,
                    data="\n".join(self._cur_data_lines),
                    id=self._cur_id,
                    retry=self._cur_retry,
                )
                self._cur_event = "message"
                self._cur_data_lines = []
                yield msg
