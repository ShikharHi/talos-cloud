"""
Talos Cloud — Idempotency Key & Request Hash Tests.
"""

import uuid
import pytest
from app.models.accounts import Account
from app.services.idempotency_service import IdempotencyService, compute_request_hash


@pytest.mark.asyncio
async def test_idempotency_key_reuse_rejection(db_session):
    user_id = uuid.uuid4()
    account = Account(account_id=user_id, email=f"idemp_{user_id}@example.com")
    db_session.add(account)
    await db_session.flush()
    key = "idemp_test_key_123"

    payload1 = {"model": "gpt-4o", "messages": [{"role": "user", "content": "Hello"}]}
    payload2 = {"model": "gpt-4o", "messages": [{"role": "user", "content": "Different payload"}]}

    # 1. First request succeeds
    rec1, is_new1 = await IdempotencyService.check_or_lock(
        db=db_session,
        user_id=user_id,
        key=key,
        request_path="/v1/llm/chat/completions",
        request_data=payload1,
    )
    assert is_new1 is True
    assert rec1.status == "PENDING"

    # 2. Second request with SAME payload hash returns existing record
    rec2, is_new2 = await IdempotencyService.check_or_lock(
        db=db_session,
        user_id=user_id,
        key=key,
        request_path="/v1/llm/chat/completions",
        request_data=payload1,
    )
    assert is_new2 is False
    assert rec2.id == rec1.id

    # 3. Third request with DIFFERENT payload hash raises ValueError
    with pytest.raises(ValueError, match="modified payload hash"):
        await IdempotencyService.check_or_lock(
            db=db_session,
            user_id=user_id,
            key=key,
            request_path="/v1/llm/chat/completions",
            request_data=payload2,
        )
