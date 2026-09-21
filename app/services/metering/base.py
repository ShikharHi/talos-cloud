"""
Talos Cloud — Metering base: UsageEvent dataclass.

This dataclass is the normalized output of every metering adapter.
The billing pipeline:
  Provider response → MeteringAdapter → UsageEvent → PricingCalculator → CreditCharge → WalletEngine

INVARIANTS:
  - provider and model_id are INTERNAL ONLY. Never include in API responses.
  - Every provider call MUST produce exactly one UsageEvent per unit_type.
    (An LLM call may produce multiple events: one for input_tokens, one for output_tokens)
  - quantity must be >= 0. If a call returns 0 tokens, quantity = 0, credits_charged = 0.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class MeteringEvent:
    """
    Normalized representation of one unit of provider usage.
    Emitted by metering adapters (llm_adapter, search_adapter, etc.).

    Multiple MeteringEvents can come from a single provider call.
    Example: an LLM call produces:
      - MeteringEvent(unit_type="input_tokens", quantity=1500)
      - MeteringEvent(unit_type="output_tokens", quantity=500)

    All fields except task_id, capability_id, unit_type, quantity are
    filled in by the pricing pipeline, not the adapter.
    """
    # Abstract capability — safe to expose
    capability_id: str

    # INTERNAL ONLY — never expose to clients
    provider: str
    model_id: str | None

    # Unit of measurement
    unit_type: str   # matches UnitType enum values

    # How many units were consumed
    quantity: int

    # Optional task context
    task_id: str | None = None

    # Provider cost — filled in by ProviderCostCalculator
    provider_cost_usd: float | None = None

    # Credits charged — filled in by CapabilityPricingEngine
    credits_charged: int = 0

    # Pricing version active at charge time
    pricing_version: str = "v1"

    # Adapter-specific extra data (image quality, search type, etc.)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Timestamp of the provider call
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
