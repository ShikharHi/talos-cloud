"""
Talos Cloud — Marketplace Purchase & Entitlement Service.

Implements the 50/50 Creator Revenue Economy:
  1. Verify credit price and existing ownership.
  2. If free, directly grant entitlement without wallet deduction.
  3. If paid, atomically reserve credits in buyer's wallet.
  4. Settle buyer spend in wallet engine and commit append-only transaction.
  5. Allocate 50% Platform revenue / 50% Creator earnings in immutable ledgers.
  6. Issue MarketplaceEntitlement so user permanently owns the item.
  7. Handle refunds via compensating transactions without mutating historical records.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any, Dict, List, Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import get_settings
from app.domain.marketplace.errors import MarketplaceError
from app.models.accounts import Account
from app.models.ledger import TransactionType
from app.models.marketplace import (
    CreatorEarning,
    MarketplaceEntitlement,
    MarketplaceListing,
    MarketplacePackageVersion,
)
from app.services.wallet_engine import InsufficientCreditsError, WalletEngine

logger = logging.getLogger("talos.marketplace.purchase")


class PurchaseService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.wallet_engine = WalletEngine(db)
        self.settings = get_settings()

    async def get_entitlement(
        self, account_id: uuid.UUID, listing_id: uuid.UUID
    ) -> Optional[MarketplaceEntitlement]:
        stmt = select(MarketplaceEntitlement).where(
            MarketplaceEntitlement.account_id == account_id,
            MarketplaceEntitlement.listing_id == listing_id,
            MarketplaceEntitlement.status == "active",
        )
        res = await self.db.execute(stmt)
        return res.scalar_one_or_none()

    async def list_entitlements(self, account_id: uuid.UUID) -> List[MarketplaceEntitlement]:
        stmt = (
            select(MarketplaceEntitlement)
            .where(
                MarketplaceEntitlement.account_id == account_id,
                MarketplaceEntitlement.status == "active",
            )
            .options(selectinload(MarketplaceEntitlement.listing))
            .order_by(MarketplaceEntitlement.acquired_at.desc())
        )
        res = await self.db.execute(stmt)
        return list(res.scalars().all())

    async def purchase_listing(
        self,
        buyer: Account,
        listing_id: uuid.UUID,
        idempotency_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Purchases a marketplace item with Talos credits and records entitlements and earnings.
        """
        stmt = (
            select(MarketplaceListing)
            .where(MarketplaceListing.listing_id == listing_id)
            .options(selectinload(MarketplaceListing.package_versions))
            .with_for_update()
        )
        res = await self.db.execute(stmt)
        listing = res.scalar_one_or_none()
        if not listing:
            raise MarketplaceError("Marketplace listing not found.")

        # Check existing entitlement (re-installation/re-acquire is free)
        existing = await self.get_entitlement(buyer.account_id, listing.listing_id)
        if existing:
            return {
                "ok": True,
                "already_owned": True,
                "entitlement_id": str(existing.entitlement_id),
                "purchase_id": existing.purchase_id,
                "price_paid_credits": existing.price_paid_credits,
            }

        # Extract scalar listing fields and buyer account id before wallet calls (which may expire session objects)
        buyer_account_id = buyer.account_id
        listing_id_val = listing.listing_id
        listing_slug = listing.slug
        listing_display_name = listing.display_name
        listing_author_id = listing.author_account_id
        listing_version_policy = listing.version_policy
        price = listing.price_credits if listing.pricing_type == "paid" else 0
        latest_ver_id = listing.package_versions[0].version_id if listing.package_versions else None
        purchase_id = f"pur_{secrets.token_urlsafe(24)}"
        reservation = None

        if price > 0:
            # 1. Atomic reservation hold on buyer's wallet
            r_key = idempotency_key or f"reserve_pur_{purchase_id}"
            try:
                reservation = await self.wallet_engine.reserve(
                    account_id=buyer_account_id,
                    task_id=f"purchase_{listing_slug}",
                    amount=price,
                    idempotency_key=r_key,
                )
            except InsufficientCreditsError as e:
                raise MarketplaceError(
                    f"Insufficient credits to purchase '{listing_display_name}'. Required: {price}, Available: {e.current} Credits."
                )

            # 2. Commit charge against buyer wallet
            await self.wallet_engine.commit(
                reservation_id=reservation.reservation_id,
                actual_amount=price,
                txn_type=TransactionType.marketplace_purchase,
                action_type="marketplace_purchase",
                reference_id=purchase_id,
                note=f"Marketplace purchase of {listing_slug} for {price} credits",
            )

        # Re-fetch listing or work with extracted identifiers to ensure clean session state
        listing_stmt = select(MarketplaceListing).where(MarketplaceListing.listing_id == listing_id_val)
        listing_res = await self.db.execute(listing_stmt)
        fresh_listing = listing_res.scalar_one_or_none()
        if not fresh_listing:
            raise MarketplaceError("Marketplace listing not found.")

        # 3. Calculate 50/50 Creator Revenue Split
        creator_share_bps = self.settings.creator_share_bps  # default 5000 = 50%
        creator_credits = (price * creator_share_bps) // 10000
        platform_credits = price - creator_credits

        # 4. Record Creator Earning in immutable accounting ledger
        if price > 0:
            earning = CreatorEarning(
                creator_id=listing_author_id,
                listing_id=listing_id_val,
                version_id=latest_ver_id,
                purchase_id=purchase_id,
                gross_credits=price,
                platform_share_credits=platform_credits,
                creator_share_credits=creator_credits,
                status="available",
            )
            self.db.add(earning)

        # 5. Issue immutable Entitlement
        entitlement = MarketplaceEntitlement(
            account_id=buyer_account_id,
            listing_id=listing_id_val,
            purchase_id=purchase_id,
            price_paid_credits=price,
            version_policy=listing_version_policy,
            status="active",
        )
        self.db.add(entitlement)

        # 6. Increment purchase counter
        fresh_listing.purchase_count += 1
        await self.db.flush()

        logger.info(
            "Account %s acquired listing %s for %d credits (purchase_id=%s)",
            buyer_account_id, fresh_listing.slug, price, purchase_id
        )

        return {
            "ok": True,
            "status": "active",
            "entitlement_id": str(entitlement.entitlement_id),
            "purchase_id": purchase_id,
            "price_paid_credits": price,
            "listing_slug": fresh_listing.slug,
            "display_name": fresh_listing.display_name,
        }

    async def refund_purchase(
        self,
        admin_or_owner: Account,
        purchase_id: str,
        reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Compensating refund: Reverses creator and platform shares, returns credits to buyer,
        and revokes entitlement without mutating historical rows.
        """
        stmt = select(MarketplaceEntitlement).where(MarketplaceEntitlement.purchase_id == purchase_id)
        res = await self.db.execute(stmt)
        entitlement = res.scalar_one_or_none()
        if not entitlement or entitlement.status != "active":
            raise MarketplaceError("Active purchase entitlement not found.")

        # Reverse creator earnings
        earning_stmt = select(CreatorEarning).where(CreatorEarning.purchase_id == purchase_id)
        earning_res = await self.db.execute(earning_stmt)
        earning = earning_res.scalar_one_or_none()
        if earning:
            earning.status = "refunded"

        # Refund credits back to buyer's topup wallet
        if entitlement.price_paid_credits > 0:
            await self.wallet_engine.refund_credits(
                account_id=entitlement.account_id,
                amount=entitlement.price_paid_credits,
                reference_id=purchase_id,
                note=f"Refund for marketplace purchase {purchase_id}: {reason or 'Requested'}",
            )

        entitlement.status = "refunded"
        await self.db.flush()

        return {
            "ok": True,
            "purchase_id": purchase_id,
            "refunded_credits": entitlement.price_paid_credits,
            "status": "refunded",
        }
