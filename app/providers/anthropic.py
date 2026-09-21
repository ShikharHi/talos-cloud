"""
Talos Cloud — Anthropic Provider Adapter.
"""

from typing import Any, AsyncGenerator, Dict, Optional, Tuple
from app.providers.base import BaseProviderAdapter, UsageResult


class AnthropicAdapter(BaseProviderAdapter):
    """
    Adapter for Anthropic Messages API.
    """

    def estimate_usage(self, messages: list, max_tokens: Optional[int] = 1000) -> UsageResult:
        # Rough estimation: 4 chars per token
        total_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
        est_input = max(10, total_chars // 4)
        est_output = max_tokens or 1000
        return UsageResult(input_tokens=est_input, output_tokens=est_output)

    async def execute_request(
        self,
        model: str,
        messages: list,
        temperature: float = 0.7,
        max_tokens: Optional[int] = 1000,
        **kwargs: Any,
    ) -> Tuple[Dict[str, Any], UsageResult]:
        # Simple proxy representation for provider execution
        est = self.estimate_usage(messages, max_tokens)
        input_tokens = est.input_tokens
        output_tokens = min(200, est.output_tokens)
        cached_tokens = kwargs.get("cached_tokens", 0)

        usage = UsageResult(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cached_input_tokens=cached_tokens,
            raw_usage={"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_input_tokens": cached_tokens},
        )

        response_body = {
            "id": "msg_anthropic_mock",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [{"type": "text", "text": "Anthropic completion response"}],
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "cache_read_input_tokens": cached_tokens},
        }

        return response_body, usage

    async def stream_request(
        self,
        model: str,
        messages: list,
        temperature: float = 0.7,
        max_tokens: Optional[int] = 1000,
        **kwargs: Any,
    ) -> AsyncGenerator[Tuple[Dict[str, Any], Optional[UsageResult]], None]:
        est = self.estimate_usage(messages, max_tokens)
        chunk = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Anthropic streamed text response"},
        }
        yield chunk, None

        final_usage = UsageResult(
            input_tokens=est.input_tokens,
            output_tokens=150,
            cached_input_tokens=0,
            raw_usage={"input_tokens": est.input_tokens, "output_tokens": 150},
        )
        final_chunk = {"type": "message_stop"}
        yield final_chunk, final_usage
