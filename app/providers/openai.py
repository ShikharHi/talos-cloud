"""
Talos Cloud — OpenAI Provider Adapter.
"""

from typing import Any, AsyncGenerator, Dict, Optional, Tuple
from app.providers.base import BaseProviderAdapter, UsageResult


class OpenAIAdapter(BaseProviderAdapter):
    """
    Adapter for OpenAI Chat Completions API.
    """

    def estimate_usage(self, messages: list, max_tokens: Optional[int] = 1000) -> UsageResult:
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
        est = self.estimate_usage(messages, max_tokens)
        input_tokens = est.input_tokens
        output_tokens = min(250, est.output_tokens)
        reasoning_tokens = 50 if ("o1" in model or "o3" in model) else 0

        usage = UsageResult(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reasoning_tokens=reasoning_tokens,
            raw_usage={"prompt_tokens": input_tokens, "completion_tokens": output_tokens, "reasoning_tokens": reasoning_tokens},
        )

        response_body = {
            "id": "chatcmpl-openai-mock",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "OpenAI completion response"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            },
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
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"content": "OpenAI streamed text response"}, "finish_reason": None}],
        }
        yield chunk, None

        final_usage = UsageResult(
            input_tokens=est.input_tokens,
            output_tokens=180,
            reasoning_tokens=0,
            raw_usage={"prompt_tokens": est.input_tokens, "completion_tokens": 180},
        )
        final_chunk = {
            "id": "chatcmpl-stream",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield final_chunk, final_usage
