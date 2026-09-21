"""
Talos Cloud — Google Provider Adapter.
"""

from typing import Any, AsyncGenerator, Dict, Optional, Tuple
from app.providers.base import BaseProviderAdapter, UsageResult


class GoogleAdapter(BaseProviderAdapter):
    """
    Adapter for Google Gemini API.
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
        output_tokens = min(200, est.output_tokens)

        usage = UsageResult(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            raw_usage={"prompt_token_count": input_tokens, "candidates_token_count": output_tokens},
        )

        response_body = {
            "candidates": [
                {
                    "content": {"parts": [{"text": "Gemini response text"}], "role": "model"},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": input_tokens,
                "candidatesTokenCount": output_tokens,
                "totalTokenCount": input_tokens + output_tokens,
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
            "candidates": [{"content": {"parts": [{"text": "Gemini streamed text"}]}}],
        }
        yield chunk, None

        final_usage = UsageResult(
            input_tokens=est.input_tokens,
            output_tokens=160,
            raw_usage={"promptTokenCount": est.input_tokens, "candidatesTokenCount": 160},
        )
        final_chunk = {
            "candidates": [{"content": {"parts": []}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": est.input_tokens, "candidatesTokenCount": 160},
        }
        yield final_chunk, final_usage
