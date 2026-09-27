"""
Talos Cloud — Google Gemini OpenAI-compatible adapter.

Uses Google's OpenAI-compatible Chat Completions endpoint so Talos can pass
function tools and consume streamed tool-call deltas without format loss.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger("talos.adapters.gemini")


class GeminiAdapter:
    def __init__(
        self,
        primary_key: str,
        secondary_key: Optional[str] = None,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai",
    ) -> None:
        self.primary_key = primary_key
        self.secondary_key = secondary_key
        self.base_url = base_url.rstrip("/")

    def format_request(
        self,
        model_id: str,
        payload: dict[str, Any],
        stream: bool = False,
    ) -> Tuple[str, dict[str, str], dict[str, Any]]:
        """Build an OpenAI-compatible Gemini request, retaining tool schemas."""
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.primary_key}",
            "Content-Type": "application/json",
        }
        body = dict(payload)
        body["model"] = model_id.removeprefix("models/")
        if stream:
            body["stream"] = True
        return url, headers, body

    def get_rotated_url(self, model_id: str, stream: bool = False, use_secondary: bool = False) -> str:
        """Returns the OpenAI-compatible endpoint; retained for adapter API compatibility."""
        return f"{self.base_url}/chat/completions"

    def get_rotated_headers(self, use_secondary: bool = False) -> dict[str, str]:
        """Returns headers using the secondary key after a 401 response."""
        key = self.secondary_key if (use_secondary and self.secondary_key) else self.primary_key
        return {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def extract_gemini_usage(gemini_response: dict[str, Any]) -> dict[str, int]:
        """Extracts token counts from Gemini usageMetadata."""
        meta = gemini_response.get("usageMetadata", {})
        return {
            "prompt_tokens": meta.get("promptTokenCount", 0),
            "completion_tokens": meta.get("candidatesTokenCount", 0),
            "total_tokens": meta.get("totalTokenCount", 0),
        }
