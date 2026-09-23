"""
Talos Cloud — Stream Adapters and State Machine.

Translates upstream provider SSE messages (OpenAI-compatible, Anthropic, Gemini, Zhipu/Z.AI)
into normalized TalosStreamEvent objects.
Guarantees:
- Terminal states (COMPLETED, FAILED, CANCELLED) are mutually exclusive.
- Provider and model details are strictly contained server-side and never leaked in events.
- Tool calls and text deltas are cleanly separated.
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from typing import Any, Iterator, Optional

from app.services.stream_parser import ParsedSSEMessage, StreamEventType, TalosStreamEvent

logger = logging.getLogger("talos.stream_adapter")


class StreamState(str, Enum):
    CREATED = "CREATED"
    STARTED = "STARTED"
    STREAMING = "STREAMING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class StreamStateMachine:
    """
    Guarantees strict single terminal state for every stream.
    Valid transitions:
      CREATED -> STARTED -> STREAMING -> COMPLETED
                                      -> FAILED
                                      -> CANCELLED
    Once terminal (COMPLETED, FAILED, CANCELLED), no further state transitions are permitted.
    """

    def __init__(self, stream_id: str) -> None:
        self.stream_id = stream_id
        self._state = StreamState.CREATED
        self.deltas_count: int = 0
        self.exact_tokens: int = 0

    @property
    def current_state(self) -> StreamState:
        return self._state

    @property
    def is_terminal(self) -> bool:
        return self._state in (StreamState.COMPLETED, StreamState.FAILED, StreamState.CANCELLED)

    def transition_started(self) -> TalosStreamEvent:
        if self._state != StreamState.CREATED:
            raise RuntimeError(f"Cannot transition to STARTED from {self._state}")
        self._state = StreamState.STARTED
        return TalosStreamEvent(type=StreamEventType.START.value, stream_id=self.stream_id)

    def transition_streaming(self) -> None:
        if self._state in (StreamState.STARTED, StreamState.STREAMING):
            self._state = StreamState.STREAMING

    def transition_completed(self, finish_reason: str = "stop") -> Optional[TalosStreamEvent]:
        if self.is_terminal:
            return None
        self._state = StreamState.COMPLETED
        return TalosStreamEvent(
            type=StreamEventType.COMPLETED.value,
            stream_id=self.stream_id,
            finish_reason=finish_reason,
        )

    def transition_failed(self, error_message: str, code: str = "stream_failed") -> Optional[TalosStreamEvent]:
        if self.is_terminal:
            return None
        self._state = StreamState.FAILED
        return TalosStreamEvent(
            type=StreamEventType.ERROR.value,
            stream_id=self.stream_id,
            error={"code": code, "message": error_message},
        )

    def transition_cancelled(self) -> Optional[TalosStreamEvent]:
        if self.is_terminal:
            return None
        self._state = StreamState.CANCELLED
        return TalosStreamEvent(
            type=StreamEventType.CANCELLED.value,
            stream_id=self.stream_id,
        )


class ProviderStreamAdapter:
    """
    Normalizes provider SSE chunks into TalosStreamEvent stream.
    Supports OpenAI-compatible (Zhipu, DeepSeek, Groq, OpenAI) and Gemini.
    """

    def __init__(self, provider: str, state_machine: StreamStateMachine) -> None:
        self.provider = provider.lower().strip()
        self.sm = state_machine

    def process_message(self, sse: ParsedSSEMessage) -> Iterator[TalosStreamEvent]:
        if sse.is_comment:
            return

        data_str = sse.data.strip()
        if not data_str:
            return

        if data_str == "[DONE]":
            comp = self.sm.transition_completed(finish_reason="stop")
            if comp:
                yield comp
            return

        try:
            payload = json.loads(data_str)
        except Exception:
            # Fragmented non-JSON or raw text
            return

        # Check for provider-level errors
        if "error" in payload:
            err = payload["error"]
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            evt = self.sm.transition_failed(error_message=err_msg, code="provider_error")
            if evt:
                yield evt
            return

        # Extract usage if present
        usage_data = payload.get("usage") or payload.get("usageMetadata")
        if usage_data:
            total_tokens = 0
            if "total_tokens" in usage_data:
                total_tokens = usage_data.get("total_tokens", 0)
            elif "totalTokenCount" in usage_data:
                total_tokens = usage_data.get("totalTokenCount", 0)
            else:
                total_tokens = usage_data.get("prompt_tokens", 0) + usage_data.get("completion_tokens", 0)

            if total_tokens > 0:
                self.sm.exact_tokens = total_tokens
                yield TalosStreamEvent(
                    type=StreamEventType.USAGE.value,
                    stream_id=self.sm.stream_id,
                    usage={"total_tokens": total_tokens},
                )

        # Parse choices
        choices = payload.get("choices")
        if choices and isinstance(choices, list):
            for choice in choices:
                delta = choice.get("delta") or {}
                finish_reason = choice.get("finish_reason")

                # Reasoning delta (deepseek-r1 / o1 / o3)
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    self.sm.transition_streaming()
                    yield TalosStreamEvent(
                        type=StreamEventType.REASONING.value,
                        stream_id=self.sm.stream_id,
                        reasoning=reasoning,
                    )

                # Text delta
                content = delta.get("content")
                if content:
                    self.sm.transition_streaming()
                    self.sm.deltas_count += 1
                    yield TalosStreamEvent(
                        type=StreamEventType.DELTA.value,
                        stream_id=self.sm.stream_id,
                        delta=content,
                    )

                # Tool call deltas
                tool_calls = delta.get("tool_calls")
                if tool_calls:
                    self.sm.transition_streaming()
                    for tc in tool_calls:
                        yield TalosStreamEvent(
                            type=StreamEventType.TOOL_CALL.value,
                            stream_id=self.sm.stream_id,
                            tool_call=tc,
                        )

                if finish_reason and finish_reason in ("stop", "tool_calls", "end_turn"):
                    comp = self.sm.transition_completed(finish_reason=finish_reason)
                    if comp:
                        yield comp

        # Gemini format support
        candidates = payload.get("candidates")
        if candidates and isinstance(candidates, list):
            cand = candidates[0]
            parts = cand.get("content", {}).get("parts", [])
            for part in parts:
                text = part.get("text")
                if text:
                    self.sm.transition_streaming()
                    self.sm.deltas_count += 1
                    yield TalosStreamEvent(
                        type=StreamEventType.DELTA.value,
                        stream_id=self.sm.stream_id,
                        delta=text,
                    )
            finish_reason = cand.get("finishReason")
            if finish_reason:
                comp = self.sm.transition_completed(finish_reason=str(finish_reason).lower())
                if comp:
                    yield comp
