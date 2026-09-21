"""
Talos Cloud — Pricing Simulator.

Allows admins to model the financial impact of different usage mixes
BEFORE publishing a new capability_pricing version.

Workflow:
  1. Admin selects pricing_version + provider_pricing_version
  2. Admin enters expected usage_mix {capability_id: fraction} (must sum to ~1.0)
  3. Simulator calculates projected blended margin
  4. Admin compares scenarios (e.g. "normal" vs "reasoning-heavy")
  5. Only if margin is acceptable: admin publishes new capability_pricing via admin API

Results are persisted in margin_simulations table for comparison and audit.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.capability_pricing import CapabilityPricing
from app.models.provider_pricing import ProviderPricing
from app.models.provider_mapping import ProviderMapping
from app.models.margin_simulation import MarginSimulation
from app.models.pricing_configuration import PricingConfiguration

logger = logging.getLogger(__name__)


class PricingSimulator:
    """
    Simulates blended margin for a given usage mix and pricing schedule.
    All results are persisted to margin_simulations for audit/comparison.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def get_credit_reference_usd(self) -> float:
        """Loads active credit reference USD value from database."""
        try:
            res = await self.db.execute(
                select(PricingConfiguration).where(PricingConfiguration.active.is_(True)).order_by(PricingConfiguration.created_at.desc())
            )
            cfg = res.scalar_one_or_none()
            if cfg and cfg.credit_reference_usd:
                return float(cfg.credit_reference_usd)
        except Exception as e:
            logger.debug(f"Could not load pricing configuration from DB: {e}")
        return 0.10

    async def simulate(
        self,
        name: str,
        usage_mix: dict[str, float],
        pricing_version: str = "v1",
        provider_pricing_version: str | None = None,
        description: str | None = None,
        created_by: uuid.UUID | None = None,
        reference_credits_per_month: int = 10_000,
    ) -> MarginSimulation:
        """
        Runs a pricing simulation and persists the result.

        usage_mix: dict mapping capability_id -> fraction of total usage (should sum to ~1.0).
        reference_credits_per_month: hypothetical total credits consumed by a user
          in a month (used to scale revenue/cost projections).

        Returns a persisted MarginSimulation row.
        """
        total_weight = sum(usage_mix.values())
        if total_weight <= 0:
            raise ValueError("usage_mix weights must be positive and sum to > 0")
        normalized_mix = {k: v / total_weight for k, v in usage_mix.items()}

        cap_pricing_rows = await self._load_capability_pricing(pricing_version)
        provider_map = await self._load_provider_mapping()
        provider_pricing = await self._load_provider_pricing(provider_pricing_version)
        credit_ref = await self.get_credit_reference_usd()

        capability_results: dict[str, dict[str, Any]] = {}
        total_projected_credits = reference_credits_per_month
        total_projected_revenue = 0.0
        total_projected_cost = 0.0

        for cap_id, fraction in normalized_mix.items():
            cap_credits = total_projected_credits * fraction

            cap_result = await self._simulate_capability(
                capability_id=cap_id,
                credit_volume=cap_credits,
                cap_pricing_rows=cap_pricing_rows,
                provider_map=provider_map,
                provider_pricing=provider_pricing,
                credit_reference_usd=credit_ref,
            )

            capability_results[cap_id] = cap_result
            total_projected_revenue += cap_result["projected_revenue_usd"]
            total_projected_cost += cap_result["projected_cost_usd"]

        # Variable cost overhead: hosting (10%) + payment gateway (5%)
        variable_overhead = total_projected_cost * 0.15
        total_variable_cost = total_projected_cost + variable_overhead

        projected_margin = (
            (total_projected_revenue - total_variable_cost) / total_projected_revenue
            if total_projected_revenue > 0 else 0.0
        )

        simulation_input = {
            "usage_mix": usage_mix,
            "normalized_mix": normalized_mix,
            "reference_credits_per_month": reference_credits_per_month,
            "pricing_version": pricing_version,
            "provider_pricing_version": provider_pricing_version,
            "credit_reference_usd": credit_ref,
        }
        simulation_output = {
            "capability_results": capability_results,
            "total_projected_revenue_usd": round(total_projected_revenue, 6),
            "total_projected_provider_cost_usd": round(total_projected_cost, 6),
            "total_variable_cost_usd": round(total_variable_cost, 6),
            "projected_blended_margin": round(projected_margin * 100, 2),
            "margin_status": self._margin_status(projected_margin),
        }

        sim = MarginSimulation(
            name=name,
            description=description,
            pricing_version=pricing_version,
            provider_pricing_version=provider_pricing_version or "latest",
            usage_mix=usage_mix,
            simulation_input=simulation_input,
            simulation_output=simulation_output,
            projected_revenue=total_projected_revenue,
            projected_provider_cost=total_projected_cost,
            projected_variable_cost=total_variable_cost,
            projected_margin=projected_margin,
            created_by=created_by,
        )
        self.db.add(sim)
        await self.db.flush()

        logger.info(
            "Simulation '%s' complete: blended_margin=%.1f%% (status=%s)",
            name, projected_margin * 100, self._margin_status(projected_margin),
        )
        return sim

    # ─── Internals ─────────────────────────────────────────────────────────────

    async def _simulate_capability(
        self,
        capability_id: str,
        credit_volume: float,
        cap_pricing_rows: dict,
        provider_map: dict,
        provider_pricing: dict,
        credit_reference_usd: float = 0.10,
    ) -> dict[str, Any]:
        """Calculates projected revenue and cost for one capability."""
        projected_revenue = credit_volume * credit_reference_usd

        cost_per_credit = self._estimate_cost_per_credit(
            capability_id, cap_pricing_rows, provider_map, provider_pricing
        )
        projected_cost = credit_volume * cost_per_credit

        capability_margin = (
            (projected_revenue - projected_cost) / projected_revenue
            if projected_revenue > 0 else 0.0
        )

        return {
            "credit_volume": round(credit_volume, 2),
            "projected_revenue_usd": round(projected_revenue, 6),
            "projected_cost_usd": round(projected_cost, 6),
            "capability_margin_pct": round(capability_margin * 100, 2),
        }

    def _estimate_cost_per_credit(
        self,
        capability_id: str,
        cap_pricing_rows: dict,
        provider_map: dict,
        provider_pricing: dict,
    ) -> float:
        unit = "output_1k" if capability_id in ("reasoning_model", "code_model", "fast_model", "vision_model") else "per_call"
        credit_cost = cap_pricing_rows.get((capability_id, unit), 0.5)

        mapping = provider_map.get(capability_id, {})
        provider = mapping.get("provider", "openai")
        model_id = mapping.get("model_id", "gpt-4o")

        p_info = provider_pricing.get((provider, model_id), {})
        if unit == "output_1k":
            cost_per_1m = p_info.get("output_cost_usd_per_1m", 15.0)
            cost_per_unit = cost_per_1m / 1000.0
        else:
            cost_per_unit = p_info.get("tool_cost_usd", 0.008)

        if credit_cost > 0:
            return cost_per_unit / credit_cost
        return 0.005

    @staticmethod
    def _margin_status(margin: float) -> str:
        if margin >= 0.75:
            return "healthy"
        elif margin >= 0.70:
            return "acceptable"
        elif margin >= 0.60:
            return "warning"
        return "critical"

    async def _load_capability_pricing(self, pricing_version: str) -> dict[tuple[str, str], float]:
        res = await self.db.execute(
            select(CapabilityPricing).where(
                CapabilityPricing.pricing_version == pricing_version,
                CapabilityPricing.active.is_(True),
            )
        )
        return {(r.capability_id, r.unit): float(r.credit_cost) for r in res.scalars().all()}

    async def _load_provider_mapping(self) -> dict[str, dict[str, str]]:
        res = await self.db.execute(
            select(ProviderMapping).where(ProviderMapping.active.is_(True))
        )
        return {r.capability_id: {"provider": r.provider, "model_id": r.model_id} for r in res.scalars().all()}

    async def _load_provider_pricing(self, version: str | None) -> dict[tuple[str, str], dict[str, float]]:
        q = select(ProviderPricing).where(ProviderPricing.active.is_(True))
        if version:
            q = q.where(ProviderPricing.pricing_version == version)
        res = await self.db.execute(q)
        return {
            (r.provider, r.model_id): {
                "input_cost_usd_per_1m": float(r.input_cost_usd_per_1m or 3.0),
                "output_cost_usd_per_1m": float(r.output_cost_usd_per_1m or 15.0),
                "cached_input_cost_usd_per_1m": float(r.cached_input_cost_usd_per_1m or 0.30),
                "tool_cost_usd": float(r.tool_cost_usd or 0.008),
            }
            for r in res.scalars().all()
        }
