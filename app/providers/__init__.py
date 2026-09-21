"""
Talos Cloud — LLM Provider Adapters Registry.
"""

from app.providers.base import BaseProviderAdapter, UsageResult
from app.providers.anthropic import AnthropicAdapter
from app.providers.openai import OpenAIAdapter
from app.providers.google import GoogleAdapter


def get_provider_adapter(model: str) -> BaseProviderAdapter:
    """
    Select provider adapter based on model prefix/name.
    """
    m = model.lower()
    if "claude" in m or "anthropic" in m:
        return AnthropicAdapter()
    elif "gemini" in m or "google" in m:
        return GoogleAdapter()
    else:
        return OpenAIAdapter()
