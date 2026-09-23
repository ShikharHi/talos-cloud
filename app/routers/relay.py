"""
Talos Cloud — Relay router.

INVARIANT: `provider` field MUST NEVER appear in any response body from
any endpoint in this router. Tests enforce this at the HTTP response level.
See tests/test_relay.py::test_provider_field_never_in_relay_response.
"""

import json
import os
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.services.auth_service import get_account_for_token
from app.services.concurrency_limiter import (
    acquire_concurrency,
    acquire_concurrency_lease,
    check_and_acquire_concurrency,
    release_concurrency,
    release_concurrency_lease,
)
from app.services.relay_service import InsufficientCreditsError, RelayService
from app.services.stream_buffer import replay_buffer
from app.services.stream_parser import TalosStreamEvent, StreamEventType

router = APIRouter(prefix="/relay", tags=["relay"])
router_v1 = APIRouter(prefix="/api/v1/llm", tags=["llm-relay-v1"])


# ─── Request / Response models ────────────────────────────────────────────────

class RelayCallRequest(BaseModel):
    capability_id: str
    payload: dict
    worst_case_units: int
    task_id: str | None = None


class RelayCallResponse(BaseModel):
    """
    INVARIANT: `provider` is NOT a field here. It must never be added.
    The test test_provider_field_never_in_relay_response asserts that 'provider'
    does not appear anywhere in the serialized response JSON.
    """
    result: dict
    credits_charged: int
    capability_id: str
    # NOTE: Do NOT add 'provider' here. It is internal-only. See PricingEvent docstring.


# ─── Auth dependency ──────────────────────────────────────────────────────────

async def get_authenticated_account(
    authorization: str = Header(...),
    db: AsyncSession = Depends(get_db),
):
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing Bearer token.")
    raw_token = authorization.removeprefix("Bearer ").strip()

    # 1. Try standard device token (bcrypt hash)
    account = await get_account_for_token(db, raw_token)
    if account is not None:
        return account

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired device token.")



# ─── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/call", response_model=RelayCallResponse)
@router_v1.post("/call", response_model=RelayCallResponse)
async def relay_call(
    req: RelayCallRequest,
    account=Depends(get_authenticated_account),
    db: AsyncSession = Depends(get_db),
):
    """
    Non-streaming relay call. Pre-checks credits, dispatches to provider,
    reconciles actual vs worst-case, returns result.
    Guarded by per-account tier concurrency limiter.
    Provider name is NEVER in the response.
    """
    tier = getattr(account, "subscription_tier", "free") or "free"
    async with check_and_acquire_concurrency(account.account_id, tier):
        service = RelayService(db)
        try:
            result = await service.call(
                account_id=account.account_id,
                task_id=req.task_id,
                capability_id=req.capability_id,
                payload=req.payload,
                worst_case_units=req.worst_case_units,
            )
        except InsufficientCreditsError as e:
            raise HTTPException(
                status_code=status.HTTP_402_PAYMENT_REQUIRED,
                detail={
                    "error": "insufficient_credits",
                    "required_credits": e.required,
                    "current_balance": e.current,
                    # NOTE: no 'provider' field here
                },
            )
        except ValueError as e:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

        # Safety check: assert provider not leaked (belt-and-suspenders)
        assert "provider" not in result, "CRITICAL: provider field leaked into relay response"
        return RelayCallResponse(**result)


@router.post("/stream")
@router_v1.post("/stream")
async def relay_stream(
    req: RelayCallRequest,
    request: Request,
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    account=Depends(get_authenticated_account),
    db: AsyncSession = Depends(get_db),
):
    """
    Streaming relay call. Streams Talos native events chunk-by-chunk.
    Supports SSE Last-Event-ID replay without re-charging or re-dispatching.
    Guarded by per-account tier concurrency lease with guaranteed release.
    Provider name is NEVER in any chunk.
    """
    # 1. Check replay buffer if Last-Event-ID reconnection requested
    if req.task_id and last_event_id is not None:
        try:
            seq = int(last_event_id)
            missed_chunks = await replay_buffer.get_replay_chunks(req.task_id, seq)
            if missed_chunks:
                async def _replay_stream() -> AsyncIterator[bytes]:
                    for chunk in missed_chunks:
                        yield chunk
                return StreamingResponse(_replay_stream(), media_type="text/event-stream")
        except (ValueError, TypeError):
            pass

    # 2. Acquire concurrency lease before starting stream
    tier = getattr(account, "subscription_tier", "free") or "free"
    lease_id = await acquire_concurrency_lease(account.account_id, tier)

    service = RelayService(db)

    async def _stream() -> AsyncIterator[bytes]:
        try:
            async for chunk in service.stream_call(
                account_id=account.account_id,
                task_id=req.task_id,
                capability_id=req.capability_id,
                payload=req.payload,
                worst_case_units=req.worst_case_units,
                request=request,
            ):
                yield chunk
        except InsufficientCreditsError as e:
            # Emit structured Talos error event before closing stream
            err_evt = TalosStreamEvent(
                type=StreamEventType.ERROR.value,
                stream_id=req.task_id,
                error={
                    "code": "insufficient_credits",
                    "message": f"Insufficient credits: required {e.required}, available {e.current}",
                },
            )
            yield err_evt.to_sse_bytes()
        finally:
            await release_concurrency_lease(account.account_id, lease_id)

    return StreamingResponse(_stream(), media_type="text/event-stream")

