"""
Talos Cloud — Native Google Gemini Adapter (Tasks 24 & 26).

Implements Google Gemini API (v1beta generateContent / streamGenerateContent):
- Format conversion from OpenAI messages to Gemini contents (user, model, parts).
- System instructions mapping (system_instruction).
- Generation config (temperature, maxOutputTokens, thinkingConfig).
- Usage extraction from Gemini usageMetadata (promptTokenCount, candidatesTokenCount, totalTokenCount).
- Zero-downtime key rotation: primary key + secondary rollover key on 401.
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
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
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
        """
        Translates payload to Gemini generateContent / streamGenerateContent format.
        """
        action = "streamGenerateContent" if stream else "generateContent"
        clean_model = model_id.removeprefix("models/")
        url = f"{self.base_url}/models/{clean_model}:{action}?key={self.primary_key}"

        headers = {
            "Content-Type": "application/json",
        }

        incoming_messages = payload.get("messages", [])
        contents: list[dict[str, Any]] = []
        system_text: Optional[str] = None

        for msg in incoming_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                system_text = content if isinstance(content, str) else str(content)
            elif role == "user":
                contents.append({"role": "user", "parts": [{"text": str(content)}]})
            elif role in ("assistant", "model"):
                contents.append({"role": "model", "parts": [{"text": str(content)}]})

        body: dict[str, Any] = {
            "contents": contents,
        }

        if system_text:
            body["system_instruction"] = {
                "parts": [{"text": system_text}]
            }

        gen_config: dict[str, Any] = {}
        if "temperature" in payload:
            gen_config["temperature"] = payload["temperature"]
        if "max_tokens" in payload:
            gen_config["maxOutputTokens"] = payload["max_tokens"]
        if "max_completion_tokens" in payload:
            gen_config["maxOutputTokens"] = payload["max_completion_tokens"]

        if gen_config:
            body["generationConfig"] = gen_config

        return url, headers, body

    def get_rotated_url(self, model_id: str, stream: bool = False, use_secondary: bool = False) -> str:
        """Returns API endpoint URL with secondary key if primary failed with 401."""
        action = "streamGenerateContent" if stream else "generateContent"
        clean_model = model_id.removeprefix("models/")
        key = self.secondary_key if (use_secondary and self.secondary_key) else self.primary_key
        return f"{self.base_url}/models/{clean_model}:{action}?key={key}"

    @staticmethod
    def extract_gemini_usage(gemini_response: dict[str, Any]) -> dict[str, int]:
        """Extracts token counts from Gemini usageMetadata."""
        meta = gemini_response.get("usageMetadata", {})
        return {
            "prompt_tokens": meta.get("promptTokenCount", 0),
            "completion_tokens": meta.get("candidatesTokenCount", 0),
            "total_tokens": meta.get("totalTokenCount", 0),
        }
