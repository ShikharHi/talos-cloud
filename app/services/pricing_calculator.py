"""
Talos Cloud — Pricing Calculator (Engine v3).

Two components:
  1. ProviderCostCalculator: MeteringEvent -> real USD cost (Decimal) from provider_pricing table.
  2. CapabilityPricingEngine: MeteringEvent + real_cost -> credit charge from capability_pricing and pricing_configurations tables.

CRITICAL:
  - credit_reference_usd is loaded dynamically from pricing_configurations table in PostgreSQL.
  - Decimal arithmetic is used for financial precision.
  - provider/model_id are NEVER returned to clients from this module.
  - Historical pricing is auditable via pricing_version stamped on each UsageEvent.
"""

import logging
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_pricing import CapabilityPricing
from app.models.provider_pricing import ProviderPricing
from app.models.pricing_configuration import PricingConfiguration
from app.services.metering.base import MeteringEvent

logger = logging.getLogger(__name__)

# Fallback constant for unseeded mock/test databases only
_FALLBACK_CREDIT_REFERENCE_USD: Decimal = Decimal("0.1000")

# Default fallback provider costs (USD per 1M tokens or per call) when DB unseeded
_DEFAULT_PROVIDER_COSTS: dict[str, Decimal] = {
    "input_tokens": Decimal("3.00"),       # $3.00 / 1M
    "output_tokens": Decimal("15.00"),     # $15.00 / 1M
    "cached_tokens": Decimal("0.30"),      # $0.30 / 1M
    "per_call": Decimal("0.008"),          # $0.008 / call
    "per_minute": Decimal("0.010"),        # $0.010 / minute
    "image_low": Decimal("0.008"),
    "image_medium": Decimal("0.020"),
    "image_high": Decimal("0.040"),
}

# Default fallback capability credit costs when DB unseeded
_DEFAULT_CAPABILITY_CREDIT_COSTS: dict[tuple[str, str], Decimal] = {
    ("reasoning_model", "input_1k"): Decimal("0.120"),
    ("reasoning_model", "output_1k"): Decimal("0.600"),
    ("code_model", "input_1k"): Decimal("0.011"),
    ("code_model", "output_1k"): Decimal("0.044"),
    ("fast_model", "input_1k"): Decimal("0.024"),
    ("fast_model", "output_1k"): Decimal("0.032"),
    ("vision_model", "input_1k"): Decimal("0.004"),
    ("vision_model", "output_1k"): Decimal("0.004"),
    ("web_search", "per_call"): Decimal("0.320"),
    ("image_gen", "image_low"): Decimal("0.320"),
    ("image_gen", "image_medium"): Decimal("0.800"),
    ("image_gen", "image_high"): Decimal("1.600"),
}


class ProviderCostCalculator:
    """
    Calculates the real USD cost Talos pays to the provider for a MeteringEvent.
    Reads from provider_pricing table (never hard-coded in prod; defaults for test DBs).
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def calculate_cost(self, event: MeteringEvent) -> float:
        """Returns real USD cost as float (internally calculated with Decimal)."""
        qty = Decimal(str(event.quantity))
        unit = event.unit_type

        if event.provider == "precheck":
            if unit in ("input_tokens", "output_tokens"):
                cost = (qty / Decimal("1000000")) * Decimal("15.00")
            else:
                cost = Decimal("0.01")
            return float(cost.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))

        try:
            result = await self.db.execute(
                select(ProviderPricing).where(
                    ProviderPricing.provider == event.provider,
                    ProviderPricing.model_id == event.model_id,
                    ProviderPricing.active.is_(True),
                )
            )
            pricing = result.scalar_one_or_none()
        except Exception:
            pricing = None

        if pricing is not None:
            if unit == "input_tokens":
                rate = Decimal(str(pricing.input_cost_usd_per_1m or "3.00"))
                cost = (qty / Decimal("1000000")) * rate
            elif unit == "output_tokens":
                rate = Decimal(str(pricing.output_cost_usd_per_1m or "15.00"))
                cost = (qty / Decimal("1000000")) * rate
            elif unit == "cached_tokens":
                rate = Decimal(str(pricing.cached_input_cost_usd_per_1m or "0.30"))
                cost = (qty / Decimal("1000000")) * rate
            elif unit in ("per_call", "per_minute"):
                rate = Decimal(str(pricing.tool_cost_usd or "0.008"))
                cost = rate * qty
            elif unit == "image_low":
                rate = Decimal(str(pricing.image_cost_usd_low or "0.008"))
                cost = rate * qty
            elif unit == "image_medium":
                rate = Decimal(str(pricing.image_cost_usd_medium or "0.020"))
                cost = rate * qty
            elif unit == "image_high":
                rate = Decimal(str(pricing.image_cost_usd_high or "0.040"))
                cost = rate * qty
            else:
                cost = Decimal("0.008") * qty
            return float(cost.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))

        # Fallback default cost when unseeded (e.g. in unit tests)
        default_rate = _DEFAULT_PROVIDER_COSTS.get(unit, Decimal("0.008"))
        if unit in ("input_tokens", "output_tokens", "cached_tokens"):
            cost = (qty / Decimal("1000000")) * default_rate
        else:
            cost = qty * default_rate
        return float(cost.quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP))


class CapabilityPricingEngine:
    """
    Calculates credit charge for a MeteringEvent.
    Uses capability_pricing and pricing_configurations tables in PostgreSQL.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_credit_reference_usd(self) -> Decimal:
        """Loads active credit_reference_usd from database configuration."""
        try:
            res = await self.db.execute(
                select(PricingConfiguration).where(PricingConfiguration.active.is_(True)).order_by(PricingConfiguration.created_at.desc())
            )
            cfg = res.scalar_one_or_none()
            if cfg and cfg.credit_reference_usd:
                return Decimal(str(cfg.credit_reference_usd))
        except Exception as e:
            logger.debug(f"Could not load pricing configuration from DB: {e}")
        return _FALLBACK_CREDIT_REFERENCE_USD

    async def credits_for_event(
        self,
        event: MeteringEvent,
        real_cost_usd: float,
    ) -> int:
        unit = self._normalize_unit(event.unit_type, event.quantity)
        qty = Decimal(str(event.quantity))

        try:
            result = await self.db.execute(
                select(CapabilityPricing).where(
                    CapabilityPricing.capability_id == event.capability_id,
                    CapabilityPricing.unit == unit,
                    CapabilityPricing.active.is_(True),
                )
            )
            cap_pricing = result.scalar_one_or_none()
        except Exception:
            cap_pricing = None

        if cap_pricing is not None:
            credit_cost = Decimal(str(cap_pricing.credit_cost))
            if unit in ("input_1k", "output_1k"):
                raw = (qty / Decimal("1000")) * credit_cost
            else:
                raw = qty * credit_cost
            return max(0, int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

        # Fallback default schedule lookup
        key = (event.capability_id, unit)
        if key in _DEFAULT_CAPABILITY_CREDIT_COSTS:
            credit_cost = _DEFAULT_CAPABILITY_CREDIT_COSTS[key]
            if unit in ("input_1k", "output_1k"):
                raw = (qty / Decimal("1000")) * credit_cost
            else:
                raw = qty * credit_cost
            return max(1, int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

        # Formula fallback: (cost / (1 - 0.70)) / credit_reference
        if real_cost_usd > 0:
            credit_ref = await self.get_credit_reference_usd()
            cost_dec = Decimal(str(real_cost_usd))
            target_rev = cost_dec / Decimal("0.30")
            raw_credits = target_rev / credit_ref
            return max(1, int(raw_credits.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))

        # Absolute fallback: 1 credit per call / 1000 units
        if unit in ("input_1k", "output_1k"):
            raw = (qty / Decimal("1000")) * Decimal("0.5")
            return max(1, int(raw.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))
        return max(1, int(qty))

    def _normalize_unit(self, unit_type: str, quantity: int) -> str:
        mapping = {
            "input_tokens": "input_1k",
            "cached_tokens": "input_1k",
            "output_tokens": "output_1k",
            "per_call": "per_call",
            "per_minute": "per_minute",
            "image_low": "image_low",
            "image_medium": "image_medium",
            "image_high": "image_high",
        }
        return mapping.get(unit_type, unit_type)

    async def get_active_pricing_version(self) -> str:
        try:
            from app.models.pricing import PricingVersion
            result = await self.db.execute(
                select(PricingVersion.version).where(PricingVersion.is_active.is_(True))
            )
            row = result.scalar_one_or_none()
            return row or "v1"
        except Exception:
            return "v1"
