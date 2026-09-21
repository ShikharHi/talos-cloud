"""
Talos Cloud — LLM Metering Adapter.

Extracts standardized MeteringEvent(s) from LLM provider responses.
Handles OpenAI-compatible usage objects (input_tokens, output_tokens, cached_tokens).

Output: list of MeteringEvents, one per token type with non-zero quantity.
"""

from typing import Any
from app.services.metering.base import MeteringEvent


def extract_llm_usage(
    provider_response: Any,
    capability_id: str,
    provider: str,
    model_id: str,
    task_id: str | None = None,
) -> list[MeteringEvent]:
    """
    Extract token usage from an LLM provider response.

    Handles multiple response formats:
    - OpenAI format: response["usage"]["prompt_tokens"], ["completion_tokens"]
    - Anthropic format: response["usage"]["input_tokens"], ["output_tokens"]
    - Groq format: response["usage"]["prompt_tokens"], ["completion_tokens"]
    - Integer / token count directly (test mocks)

    Returns a list of MeteringEvents (one per non-zero token type).
    """
    if isinstance(provider_response, int):
        return [MeteringEvent(
            capability_id=capability_id,
            provider=provider,
            model_id=model_id,
            unit_type="output_tokens",
            quantity=provider_response,
            task_id=task_id,
        )]

    if not isinstance(provider_response, dict):
        provider_response = {}

    events: list[MeteringEvent] = []
    usage = provider_response.get("usage") or {}

    # Normalize field names across providers
    input_tokens = (
        usage.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("prompt_token_count")
        or 0
    )
    output_tokens = (
        usage.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("candidates_token_count")
        or 0
    )
    cached_tokens = (
        usage.get("cache_read_input_tokens")
        or usage.get("cached_tokens")
        or usage.get("prompt_tokens_details", {}).get("cached_tokens")
        or 0
    )
    reasoning_tokens = (
        usage.get("completion_tokens_details", {}).get("reasoning_tokens")
        or usage.get("reasoning_tokens")
        or usage.get("thinking_tokens")
        or usage.get("candidates_token_details", {}).get("reasoning_tokens")
        or 0
    )

    if input_tokens > 0:
        events.append(MeteringEvent(
            capability_id=capability_id,
            provider=provider,
            model_id=model_id,
            unit_type="input_tokens",
            quantity=int(input_tokens),
            task_id=task_id,
            metadata={"raw_usage": usage},
        ))

    if output_tokens > 0:
        events.append(MeteringEvent(
            capability_id=capability_id,
            provider=provider,
            model_id=model_id,
            unit_type="output_tokens",
            quantity=int(output_tokens),
            task_id=task_id,
        ))

    if cached_tokens > 0:
        events.append(MeteringEvent(
            capability_id=capability_id,
            provider=provider,
            model_id=model_id,
            unit_type="cached_tokens",
            quantity=int(cached_tokens),
            task_id=task_id,
        ))

    if reasoning_tokens > 0:
        events.append(MeteringEvent(
            capability_id=capability_id,
            provider=provider,
            model_id=model_id,
            unit_type="reasoning_tokens",
            quantity=int(reasoning_tokens),
            task_id=task_id,
        ))

    # If no usage data found, emit a zero event so the call is still logged
    if not events:
        events.append(MeteringEvent(
            capability_id=capability_id,
            provider=provider,
            model_id=model_id,
            unit_type="output_tokens",
            quantity=0,
            task_id=task_id,
            metadata={"warning": "no_usage_data_in_response"},
        ))

    return events
