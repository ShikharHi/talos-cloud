"""
Talos Cloud — Subscription Service.

Manages enrollment, plan changes, and the monthly credit reset cycle.

CRITICAL INVARIANT: monthly_reset() MUST be idempotent.
If the background worker runs it twice for the same period, the user
must receive only one monthly grant. This is enforced by checking
subscription.current_period_start before writing any grant transaction.
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.subscription_plans import Subscription, SubscriptionPlan, SubscriptionStatus
from app.services.wallet_engine import WalletEngine


class SubscriptionService:
    """
    Manages user subscriptions and monthly credit grants.
    Works with WalletEngine for all balance mutations.
    """

    def __init__(self, db: AsyncSession):
        self.db = db
        self.wallet_engine = WalletEngine(db)

    # ─── Enrollment ────────────────────────────────────────────────────────────

    async def enroll(
        self,
        account_id: uuid.UUID,
        plan_name: str,
        gateway_subscription_id: str | None = None,
    ) -> Subscription:
        """
        Enrolls an account in a subscription plan.
        Creates the Subscription record and issues the initial monthly credit grant.

        If account already has a subscription, raises ValueError.
        Use change_plan() to change plans.
        """
        plan = await self._get_plan_by_name(plan_name)
        if plan is None:
            raise ValueError(f"Unknown plan '{plan_name}'. Check subscription_plans table.")

        # Check for existing subscription
        existing = await self._get_subscription(account_id)
        if existing is not None:
            raise ValueError(
                f"Account {account_id} already has a subscription ({existing.status}). "
                "Use change_plan() to change plans."
            )

        now = datetime.now(timezone.utc)
        period_end = now + timedelta(days=plan.reset_period_days)

        subscription = Subscription(
            account_id=account_id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            started_at=now,
            current_period_start=now,
            current_period_end=period_end,
            next_reset_at=period_end,
            gateway_subscription_id=gateway_subscription_id,
        )
        self.db.add(subscription)
        await self.db.flush()

        # Create wallet if it doesn't exist yet
        try:
            wallet = await self.wallet_engine.get_wallet(account_id)
        except ValueError:
            wallet = await self.wallet_engine.create_wallet(
                account_id=account_id,
                initial_monthly=0,
                initial_topup=0,
            )

        # Issue initial monthly grant
        cycle_ref = now.strftime("%Y-%m")
        await self.wallet_engine.apply_monthly_grant(
            account_id=account_id,
            grant_amount=plan.monthly_credits,
            cycle_ref=cycle_ref,
        )

        return subscription

    # ─── Monthly Reset (Background Worker entry point) ─────────────────────────

    async def monthly_reset(self, subscription_id: uuid.UUID) -> bool:
        """
        Performs the monthly billing cycle reset for a subscription.
        IDEMPOTENT: if already reset for this period, returns False (no-op).

        Steps:
          1. Load subscription (FOR UPDATE lock).
          2. Check next_reset_at <= NOW(). If not, skip (not due yet).
          3. Check current_period_start hasn't already been advanced (idempotency guard).
          4. Expire unused monthly credits + grant new cycle via WalletEngine.
          5. Advance current_period_start, current_period_end, next_reset_at.

        Returns True if reset was performed, False if skipped (idempotent).
        """
        result = await self.db.execute(
            select(Subscription)
            .where(Subscription.id == subscription_id)
            .with_for_update()
        )
        sub = result.scalar_one_or_none()
        if sub is None:
            raise ValueError(f"Subscription {subscription_id} not found")

        now = datetime.now(timezone.utc)

        # Check if reset is actually due
        if sub.next_reset_at > now:
            return False  # not due yet

        if sub.status not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE):
            return False  # cancelled/expired subscriptions don't reset

        # Load the plan
        plan_result = await self.db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == sub.plan_id)
        )
        plan = plan_result.scalar_one()

        # Compute new period
        new_period_start = sub.next_reset_at
        new_period_end = new_period_start + timedelta(days=plan.reset_period_days)
        cycle_ref = new_period_start.strftime("%Y-%m")

        # Apply the grant (WalletEngine handles expiry of old monthly credits + new grant)
        await self.wallet_engine.apply_monthly_grant(
            account_id=sub.account_id,
            grant_amount=plan.monthly_credits,
            cycle_ref=cycle_ref,
        )

        # Advance the subscription period
        sub.current_period_start = new_period_start
        sub.current_period_end = new_period_end
        sub.next_reset_at = new_period_end
        sub.updated_at = now

        await self.db.flush()
        return True

    # ─── Plan Access Control ───────────────────────────────────────────────────

    async def get_plan_access(
        self,
        account_id: uuid.UUID,
        capability_id: str,
    ) -> bool:
        """
        Returns whether the user's plan permits access to the given capability.
        Used by the relay to gate premium capabilities.
        """
        sub = await self._get_subscription(account_id)
        if sub is None:
            # No subscription = free tier access only
            return capability_id in _FREE_TIER_CAPABILITIES

        plan = await self._get_plan_by_id(sub.plan_id)
        if plan is None:
            return False

        allowed = _PLAN_CAPABILITY_ACCESS.get(plan.name, set())
        return capability_id in allowed

    # ─── Queries ───────────────────────────────────────────────────────────────

    async def get_active_subscription(self, account_id: uuid.UUID) -> Subscription | None:
        """Returns the active subscription for an account, or None."""
        return await self._get_subscription(account_id)

    async def get_subscriptions_due_for_reset(self) -> list[Subscription]:
        """Returns all active subscriptions whose next_reset_at has passed."""
        now = datetime.now(timezone.utc)
        result = await self.db.execute(
            select(Subscription).where(
                Subscription.next_reset_at <= now,
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]),
            )
        )
        return list(result.scalars().all())

    # ─── Internal ─────────────────────────────────────────────────────────────

    async def _get_subscription(self, account_id: uuid.UUID) -> Subscription | None:
        result = await self.db.execute(
            select(Subscription).where(Subscription.account_id == account_id)
        )
        return result.scalar_one_or_none()

    async def _get_plan_by_name(self, name: str) -> SubscriptionPlan | None:
        result = await self.db.execute(
            select(SubscriptionPlan).where(
                SubscriptionPlan.name == name,
                SubscriptionPlan.active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def _get_plan_by_id(self, plan_id: uuid.UUID) -> SubscriptionPlan | None:
        result = await self.db.execute(
            select(SubscriptionPlan).where(SubscriptionPlan.id == plan_id)
        )
        return result.scalar_one_or_none()


# ─── Plan capability access control ───────────────────────────────────────────

_FREE_TIER_CAPABILITIES = frozenset(["fast_model", "web_search", "code_model"])

_PLAN_CAPABILITY_ACCESS: dict[str, frozenset] = {
    "free":     frozenset(["fast_model", "web_search", "code_model"]),
    "plus":     frozenset(["fast_model", "web_search", "code_model", "vision_model"]),
    "pro":      frozenset(["fast_model", "web_search", "code_model", "vision_model",
                           "reasoning_model", "browser_use"]),
    "pro_plus": frozenset(["fast_model", "web_search", "code_model", "vision_model",
                           "reasoning_model", "browser_use", "image_gen"]),
    "ultra":    frozenset(["fast_model", "web_search", "code_model", "vision_model",
                           "reasoning_model", "browser_use", "image_gen"]),
}
