"""
Talos Cloud — Base Provider Adapter & UsageResult Dataclass.

Normalizes raw provider responses (Anthropic, OpenAI, Google) into a standardized UsageResult:
  - input_tokens
  - output_tokens
  - cached_input_tokens
  - reasoning_tokens
  - raw_usage

The central PricingEngine takes normalized UsageResult -> calculates actual provider COGS USD -> converts to Talos Credits.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Dict, Optional, Tuple


@dataclass
class UsageResult:
    """
    Standardized, provider-agnostic token usage record.
    """
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    raw_usage: Dict[str, Any] = field(default_factory=dict)


class BaseProviderAdapter(ABC):
    """
    Interface for LLM provider adapters.
    """

    @abstractmethod
    def estimate_usage(self, messages: list, max_tokens: Optional[int] = 1000) -> UsageResult:
        """Estimate worst-case input & output token counts before execution."""
        pass

    @abstractmethod
    async def execute_request(
        self,
        model: str,
        messages: list,
        temperature: float = 0.7,
        max_tokens: Optional[int] = 1000,
        **kwargs: Any,
    ) -> Tuple[Dict[str, Any], UsageResult]:
        """Execute non-streaming request and return (response_body, normalized_usage)."""
        pass

    @abstractmethod
    async def stream_request(
        self,
        model: str,
        messages: list,
        temperature: float = 0.7,
        max_tokens: Optional[int] = 1000,
        **kwargs: Any,
    ) -> AsyncGenerator[Tuple[Dict[str, Any], Optional[UsageResult]], None]:
        """Stream chunks and yield final UsageResult on completion."""
        pass
