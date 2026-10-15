from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Any, Tuple
from pydantic import BaseModel
from datetime import datetime, timezone

class PricingCalculation(BaseModel):
    resource_id: str
    pricing_version: str
    vendor_cost_usd: Decimal
    talos_credits: Decimal
    units_metered: Dict[str, Any]

class DynamicPricingService:
    @staticmethod
    def calculate_llm_cost(
        pricing_row: Dict[str, Any],
        input_tokens: int,
        output_tokens: int,
        cached_tokens: int = 0
    ) -> PricingCalculation:
        """
        Calculate authoritative vendor cost in USD and Talos credits for an LLM completion turn.
        Formula:
          vendor_cost_usd = (input * in_rate + output * out_rate + cached * cache_rate) / 1,000,000
          talos_credits = (vendor_cost_usd / 0.001) * credit_multiplier
        """
        in_rate = Decimal(str(pricing_row.get("input_rate_usd", "0")))
        out_rate = Decimal(str(pricing_row.get("output_rate_usd", "0")))
        cache_rate = Decimal(str(pricing_row.get("cached_rate_usd", "0")))
        multiplier = Decimal(str(pricing_row.get("credit_multiplier", "1.0")))

        million = Decimal("1000000")
        thousandth = Decimal("0.001")

        cost_in = (Decimal(input_tokens) * in_rate) / million
        cost_out = (Decimal(output_tokens) * out_rate) / million
        cost_cache = (Decimal(cached_tokens) * cache_rate) / million

        total_vendor_usd = (cost_in + cost_out + cost_cache).quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)
        total_credits = ((total_vendor_usd / thousandth) * multiplier).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

        return PricingCalculation(
            resource_id=pricing_row["resource_id"],
            pricing_version=pricing_row["pricing_version"],
            vendor_cost_usd=total_vendor_usd,
            talos_credits=total_credits,
            units_metered={
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cached_tokens": cached_tokens
            }
        )

    @staticmethod
    def calculate_tool_cost(
        pricing_row: Dict[str, Any],
        requests_count: int = 1
    ) -> PricingCalculation:
        """
        Calculate authoritative vendor cost in USD and Talos credits for a tool invocation (e.g. Tavily search).
        """
        tool_rate = Decimal(str(pricing_row.get("tool_call_rate_usd", "0")))
        multiplier = Decimal(str(pricing_row.get("credit_multiplier", "1.0")))
        thousandth = Decimal("0.001")

        total_vendor_usd = (Decimal(requests_count) * tool_rate).quantize(Decimal("0.000000000001"), rounding=ROUND_HALF_UP)
        total_credits = ((total_vendor_usd / thousandth) * multiplier).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

        return PricingCalculation(
            resource_id=pricing_row["resource_id"],
            pricing_version=pricing_row["pricing_version"],
            vendor_cost_usd=total_vendor_usd,
            talos_credits=total_credits,
            units_metered={"requests_count": requests_count}
        )

pricing_service = DynamicPricingService()
