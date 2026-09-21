"""
Talos Cloud — Native OpenAI Adapter (Tasks 25 & 26).

Handles:
- Native OpenAI /v1/chat/completions endpoint.
- Exact streaming token usage via `stream_options: {"include_usage": True}`.
- Parameter compatibility for reasoning models (o1, o3-mini):
  - Converts `max_tokens` to `max_completion_tokens`.
  - Omits unsupported `temperature` or pins to 1.
- Zero-downtime key rotation: primary key + secondary rollover key on 401.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger("talos.adapters.openai")

REASONING_MODELS = {"o1", "o1-mini", "o1-preview", "o3", "o3-mini"}


class OpenAIAdapter:
    def __init__(self, primary_key: str, secondary_key: Optional[str] = None, base_url: str = "https://api.openai.com/v1"):
        self.primary_key = primary_key
        self.secondary_key = secondary_key
        self.base_url = base_url.rstrip("/")

    def format_request(self, model_id: str, payload: dict[str, Any], stream: bool = False) -> Tuple[str, dict[str, str], dict[str, Any]]:
        """
        Builds URL, headers, and body for OpenAI API request with reasoning model compatibility
        and stream usage options.
        """
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.primary_key}",
            "Content-Type": "application/json",
        }

        body = dict(payload)
        body["model"] = model_id

        # Check if reasoning model
        is_reasoning = any(rm in model_id.lower() for rm in REASONING_MODELS)
        if is_reasoning:
            # Replace max_tokens with max_completion_tokens
            if "max_tokens" in body:
                body["max_completion_tokens"] = body.pop("max_tokens")
            # Remove temperature if present (o1 rejects temperature != 1)
            if "temperature" in body:
                del body["temperature"]

        if stream:
            body["stream"] = True
            # Request exact usage in final stream chunk
            body["stream_options"] = {"include_usage": True}

        return url, headers, body

    def get_rotated_headers(self, use_secondary: bool = False) -> dict[str, str]:
        """Provides headers with secondary rollover key if primary fails with 401."""
        key = self.secondary_key if (use_secondary and self.secondary_key) else self.primary_key
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
