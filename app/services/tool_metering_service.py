"""
Talos Cloud — Tool Metering & Billing Service.

Meters tool call execution across generic pricing models:
  - PER_CALL (Fixed credit cost per execution)
  - PER_UNIT (Cost scaled by data/token units)
  - PER_SECOND (Cost scaled by execution duration)
  - PROVIDER_COST (Pass-through provider cost with margin multiplier)
  - CUSTOM (Custom billing rules)
"""

import uuid
from typing import Any, Dict, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tools import ToolRequest
from app.models.usage_events import UsageEvent
from app.services.wallet_engine import WalletEngine


class ToolMeteringService:
    @staticmethod
    async def meter_and_bill_tool_call(
        db: AsyncSession,
        account_id: uuid.UUID,
        tool_name: str,
        pricing_model: str = "PER_CALL",
        usage_units: float = 1.0,
        provider_cost_usd: float = 0.0,
        rate_per_unit_credits: float = 1.0,
        run_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        request_id: Optional[str] = None,
    ) -> Tuple[ToolRequest, float]:
        """
        Calculates tool credit cost, charges wallet via WalletEngine, and logs ToolRequest & UsageEvent.
        """
        # Calculate credit cost based on pricing model
        if pricing_model == "PER_CALL":
            credit_cost = rate_per_unit_credits
        elif pricing_model in ("PER_UNIT", "PER_SECOND"):
            credit_cost = usage_units * rate_per_unit_credits
        elif pricing_model == "PROVIDER_COST":
            # $1 COGS = 10 credits multiplier
            credit_cost = provider_cost_usd * 10.0 * rate_per_unit_credits
        else:
            credit_cost = rate_per_unit_credits

        credit_cost_int = max(1, int(round(credit_cost)))
        req_id = request_id or f"tool_{uuid.uuid4()}"

        # 1. Reserve & Charge atomically via WalletEngine
        reservation = await WalletEngine.reserve(
            db=db,
            account_id=account_id,
            estimated_credits=credit_cost_int,
            task_id=run_id,
            request_id=req_id,
        )
        await WalletEngine.commit(
            db=db,
            reservation_id=reservation.reservation_id,
            actual_credits=credit_cost_int,
        )

        # 2. Log ToolRequest
        tool_req = ToolRequest(
            request_id=req_id,
            run_id=run_id,
            user_id=account_id,
            agent_id=agent_id,
            tool_name=tool_name,
            pricing_model=pricing_model,
            usage_units=usage_units,
            provider_cost_usd=provider_cost_usd,
            credit_cost=float(credit_cost_int),
            status="COMPLETED",
        )
        db.add(tool_req)

        # 3. Log Immutable UsageEvent audit
        evt = UsageEvent(
            account_id=account_id,
            run_id=run_id,
            agent_id=agent_id,
            request_id=req_id,
            provider="tool",
            capability_id=tool_name,
            provider_cost_usd=provider_cost_usd,
            credits_reserved=float(credit_cost_int),
            credits_charged=credit_cost_int,
            credits_released=0.0,
            status="COMPLETED",
        )
        db.add(evt)
        await db.commit()

        return tool_req, float(credit_cost_int)
