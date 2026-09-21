"""
Talos Cloud — Idempotency Service.

Protects billable operations (LLM proxy, credit charges, webhooks) from duplicate processing.
Verifies `key` + `request_hash`. Rejects key reuse if request payload/hash is modified.
"""

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency import IdempotencyKey


def compute_request_hash(data: Any) -> str:
    """Compute sha256 hash of normalized JSON request body."""
    if isinstance(data, (dict, list)):
        payload_str = json.dumps(data, sort_keys=True)
    else:
        payload_str = str(data)
    return hashlib.sha256(payload_str.encode("utf-8")).hexdigest()


class IdempotencyService:
    @staticmethod
    async def check_or_lock(
        db: AsyncSession,
        user_id: uuid.UUID,
        key: str,
        request_path: str,
        request_data: Any,
        ttl_minutes: int = 60,
    ) -> Tuple[Optional[IdempotencyKey], bool]:
        """
        Check if idempotency key exists.
        Returns (record, is_new).
        If record exists with DIFFERENT request_hash -> raises ValueError.
        If record exists & COMPLETED -> returns cached response.
        """
        req_hash = compute_request_hash(request_data)
        now = datetime.now(timezone.utc)

        stmt = select(IdempotencyKey).where(
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.key == key,
        )
        res = await db.execute(stmt)
        existing = res.scalar_one_or_none()

        if existing:
            if existing.request_hash != req_hash:
                raise ValueError("Idempotency key reuse with modified payload hash is forbidden")
            return existing, False

        # Create PENDING lock record
        new_record = IdempotencyKey(
            user_id=user_id,
            key=key,
            request_hash=req_hash,
            request_path=request_path,
            status="PENDING",
            locked_at=now,
            expires_at=now + timedelta(minutes=ttl_minutes),
        )
        db.add(new_record)
        await db.commit()
        await db.refresh(new_record)
        return new_record, True

    @staticmethod
    async def complete(
        db: AsyncSession,
        record_id: uuid.UUID,
        response_code: int,
        response_body: Dict[str, Any],
    ) -> None:
        """Mark idempotency record as COMPLETED with cached response."""
        now = datetime.now(timezone.utc)
        stmt = select(IdempotencyKey).where(IdempotencyKey.id == record_id)
        res = await db.execute(stmt)
        rec = res.scalar_one_or_none()
        if rec:
            rec.status = "COMPLETED"
            rec.response_code = response_code
            rec.response_body = json.dumps(response_body)
            rec.completed_at = now
            await db.commit()
