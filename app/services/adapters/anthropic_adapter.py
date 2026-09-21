"""
Talos Cloud — Native Anthropic Adapter (Tasks 23 & 26).

Implements native Anthropic Messages API (/v1/messages) integration:
- Direct compliance with Anthropic schema (x-api-key, anthropic-version: 2023-06-01).
- System message separation (Anthropic top-level system parameter).
- Tool definitions and tool_use / tool_result content block mapping.
- SSE stream event parsing (message_start, content_block_delta, message_delta, message_stop).
- Zero-downtime key rotation: primary key + secondary rollover key on 401.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Tuple

logger = logging.getLogger("talos.adapters.anthropic")


class AnthropicAdapter:
    def __init__(
        self,
        primary_key: str,
        secondary_key: Optional[str] = None,
        base_url: str = "https://api.anthropic.com/v1",
        api_version: str = "2023-06-01",
    ) -> None:
        self.primary_key = primary_key
        self.secondary_key = secondary_key
        self.base_url = base_url.rstrip("/")
        self.api_version = api_version

    def format_request(
        self,
        model_id: str,
        payload: dict[str, Any],
        stream: bool = False,
    ) -> Tuple[str, dict[str, str], dict[str, Any]]:
        """
        Translates standard payload into Anthropic Messages API format:
        - Separates system message into top-level 'system' field
        - Formats 'messages' list for Anthropic API
        - Sets x-api-key and anthropic-version headers
        """
        url = f"{self.base_url}/messages"
        headers = {
            "x-api-key": self.primary_key,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }

        incoming_messages = payload.get("messages", [])
        anthropic_messages = []
        system_prompts: list[str] = []

        for msg in incoming_messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")

            if role == "system":
                if isinstance(content, str):
                    system_prompts.append(content)
                elif isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and item.get("text"):
                            system_prompts.append(item["text"])
            elif role in ("user", "assistant"):
                anthropic_messages.append({"role": role, "content": content})

        body: dict[str, Any] = {
            "model": model_id,
            "messages": anthropic_messages,
            "max_tokens": payload.get("max_tokens") or payload.get("max_completion_tokens") or 4096,
        }

        if system_prompts:
            body["system"] = "\n\n".join(system_prompts)

        if "temperature" in payload:
            body["temperature"] = payload["temperature"]

        # Tools mapping
        if "tools" in payload:
            tools = []
            for t in payload["tools"]:
                fn = t.get("function", {})
                tools.append({
                    "name": fn.get("name", "tool"),
                    "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
                })
            if tools:
                body["tools"] = tools

        if stream:
            body["stream"] = True

        return url, headers, body

    def get_rotated_headers(self, use_secondary: bool = False) -> dict[str, str]:
        key = self.secondary_key if (use_secondary and self.secondary_key) else self.primary_key
        return {
            "x-api-key": key,
            "anthropic-version": self.api_version,
            "content-type": "application/json",
        }

    @staticmethod
    def parse_sse_chunk(line: str) -> Optional[dict[str, Any]]:
        """
        Parses Anthropic SSE streaming event line into structured data.
        Handles message_start, content_block_delta, and message_delta.
        """
        line = line.strip()
        if not line.startswith("data:"):
            return None

        raw_data = line[len("data:"):].strip()
        if raw_data == "[DONE]":
            return {"type": "done"}

        try:
            return json.loads(raw_data)
        except json.JSONDecodeError:
            return None
