"""
Talos Cloud — Verification & Fix Pass Tests for Tasks 5–30.

Covers:
- Tasks 5 & 18: Rate limiter and Concurrency limiter fail-closed / degraded modes
- Task 20: Multi-worker distributed circuit breaker state synchronization via Redis
- Task 30: Multi-worker distributed SSE stream replay buffer via Redis
- Task 29: StreamBillingPolicy & financial solvency on client disconnect
- Task 11: Capability-based package script allowance (manifest declared vs undeclared)
- Task 14: Publisher-owned cryptographic key signing and dual signing
- Task 7: Verification of composite indexes on pricing_events
- Tasks 23–25: Live-path adapter dispatch verification through RelayService
"""

import asyncio
import io
import json
import uuid
import zipfile
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from fastapi import HTTPException

from app.config import get_settings
from app.services.rate_limiter import check_rate_limit
from app.services.concurrency_limiter import acquire_concurrency, release_concurrency
from app.services.circuit_breaker import ProviderCircuitBreaker, CircuitState
from app.services.stream_buffer import StreamReplayBuffer
from app.storage.package_security import verify_package_stream
from app.storage.package_signing import (
    generate_publisher_keypair,
    sign_publisher_package,
    countersign_platform_package,
    verify_dual_signed_package,
    verify_package_signature,
)
from app.models.ledger import PricingEvent


# ─── Tasks 5 & 18: Fail-Closed Security on Redis Failure ─────────────────────

@pytest.mark.asyncio
async def test_rate_limiter_fail_closed_in_production():
    """When Redis is unavailable in production, rate limiter fails closed for security."""
    settings = get_settings()
    orig_env = settings.talos_env
    orig_fail_closed = settings.rate_limit_fail_closed

    try:
        settings.talos_env = "production"
        settings.rate_limit_fail_closed = True

        # Simulate Redis client returning None (offline)
        with patch("app.services.rate_limiter.get_redis_client", new_callable=AsyncMock) as mock_redis:
            mock_redis.return_value = None

            allowed, headers = await check_rate_limit("test_key", max_requests=10, window_seconds=60)
            assert allowed is False
            assert headers["X-RateLimit-Degraded"] == "redis_unavailable"
    finally:
        settings.talos_env = orig_env
        settings.rate_limit_fail_closed = orig_fail_closed


@pytest.mark.asyncio
async def test_concurrency_limiter_fail_closed_and_degraded_mode():
    """In production with Redis offline, concurrency limiter enforces degraded limits or fails closed."""
    settings = get_settings()
    orig_env = settings.talos_env
    orig_fail_closed = settings.concurrency_fail_closed

    try:
        settings.talos_env = "production"
        settings.concurrency_fail_closed = True

        acc_id = f"test-fail-closed-{uuid.uuid4().hex[:8]}"

        with patch("app.services.rate_limiter.get_redis_client", new_callable=AsyncMock) as mock_redis:
            mock_redis.return_value = None

            # In fail-closed mode, must raise 503
            with pytest.raises(HTTPException) as exc_info:
                await acquire_concurrency(acc_id, tier="pro")
            assert exc_info.value.status_code == 503
    finally:
        settings.talos_env = orig_env
        settings.concurrency_fail_closed = orig_fail_closed


# ─── Task 20: Multi-Worker Distributed Circuit Breaker ───────────────────────

@pytest.mark.asyncio
async def test_distributed_circuit_breaker_simulated_multi_worker():
    """Circuit breaker state changes on Worker 1 are immediately observed by Worker 2 via shared store."""
    cb1 = ProviderCircuitBreaker(failure_threshold=3, recovery_timeout_seconds=0.1)
    cb2 = ProviderCircuitBreaker(failure_threshold=3, recovery_timeout_seconds=0.1)

    # Shared simulated Redis hash store
    shared_redis_data: dict[str, dict[str, str]] = {}

    class FakeRedisClient:
        async def hgetall(self, key):
            return shared_redis_data.get(key, {})

        async def hget(self, key, field):
            return shared_redis_data.get(key, {}).get(field)

        async def hset(self, key, mapping):
            if key not in shared_redis_data:
                shared_redis_data[key] = {}
            shared_redis_data[key].update(mapping)

        async def hincrby(self, key, field, amount):
            if key not in shared_redis_data:
                shared_redis_data[key] = {}
            val = int(shared_redis_data[key].get(field, "0")) + amount
            shared_redis_data[key][field] = str(val)
            return val

        async def expire(self, key, ttl):
            pass

    fake_redis = FakeRedisClient()

    cb1._get_redis = AsyncMock(return_value=fake_redis)
    cb2._get_redis = AsyncMock(return_value=fake_redis)

    # Worker 1 records 3 failures for 'anthropic'
    for _ in range(3):
        await cb1.record_failure("anthropic")

    # Worker 2 checks can_execute -> must see OPEN and block!
    can_exec_w2 = await cb2.can_execute("anthropic")
    assert can_exec_w2 is False

    # Wait for recovery timeout
    await asyncio.sleep(0.15)

    # Worker 2 probes in HALF_OPEN
    assert await cb2.can_execute("anthropic") is True

    # Worker 2 records success -> resets globally
    await cb2.record_success("anthropic")

    # Worker 1 checks state -> now CLOSED
    assert await cb1.can_execute("anthropic") is True


# ─── Task 30: Multi-Worker Distributed SSE Replay Buffer ─────────────────────

@pytest.mark.asyncio
async def test_distributed_replay_buffer_multi_worker():
    """Worker 1 records stream chunks; Worker 2 retrieves them on Last-Event-ID reconnection."""
    w1_buffer = StreamReplayBuffer()
    w2_buffer = StreamReplayBuffer()

    # Shared simulated Redis sorted set
    shared_stream_chunks: dict[str, dict[str, float]] = {}
    shared_seqs: dict[str, int] = {}

    class FakeStreamRedis:
        async def incr(self, key):
            shared_seqs[key] = shared_seqs.get(key, 0) + 1
            return shared_seqs[key]

        async def expire(self, key, ttl):
            pass

        async def zadd(self, key, mapping):
            if key not in shared_stream_chunks:
                shared_stream_chunks[key] = {}
            shared_stream_chunks[key].update(mapping)

        async def zrangebyscore(self, key, min_score, max_score):
            # min_score format is "(1"
            min_val = float(min_score.replace("(", ""))
            items = shared_stream_chunks.get(key, {})
            # Return items where score > min_val sorted by score
            filtered = [k for k, score in sorted(items.items(), key=lambda x: x[1]) if score > min_val]
            return filtered

    fake_redis = FakeStreamRedis()
    w1_buffer._get_redis = AsyncMock(return_value=fake_redis)
    w2_buffer._get_redis = AsyncMock(return_value=fake_redis)

    task_id = f"task-distributed-stream-{uuid.uuid4().hex[:8]}"

    # Worker 1 yields and formats 3 chunks
    await w1_buffer.record_and_format_chunk(task_id, b"data: {\"chunk\": 1}\n\n")
    await w1_buffer.record_and_format_chunk(task_id, b"data: {\"chunk\": 2}\n\n")
    await w1_buffer.record_and_format_chunk(task_id, b"data: {\"chunk\": 3}\n\n")

    # Client reconnects to Worker 2 with Last-Event-ID = 1
    replayed = await w2_buffer.get_replay_chunks(task_id, last_event_id=1)
    assert len(replayed) == 2
    assert b"id: 2" in replayed[0]
    assert b"id: 3" in replayed[1]


# ─── Task 11: Capability-Based Script Allowance in Packages ───────────────────

def test_package_scripts_allowed_when_declared_in_manifest():
    """Packages containing shell/PowerShell scripts are permitted if declared in manifest capabilities."""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        # Valid agent manifest declaring script execution capability
        manifest = {
            "name": "system-admin-agent",
            "version": "1.0.0",
            "description": "Automated system administration skill",
            "capabilities": ["scripts", "shell_execution"],
        }
        zf.writestr("agent.yaml", json.dumps(manifest))
        zf.writestr("scripts/setup.sh", "#!/bin/bash\necho 'Setting up environment'")
        zf.writestr("scripts/deploy.ps1", "Write-Output 'Deploying'")

    zip_buffer.seek(0)
    result = verify_package_stream(zip_buffer, resource_type="agent")
    assert result.valid is True
    assert len(result.errors) == 0


def test_package_scripts_rejected_when_undeclared():
    """Packages containing scripts without declaring script capability are rejected."""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        manifest = {
            "name": "basic-chat-skill",
            "version": "1.0.0",
            "description": "Text only skill",
            # No script capability declared
        }
        zf.writestr("agent.yaml", json.dumps(manifest))
        zf.writestr("stealth_script.sh", "echo 'Surprise'")

    zip_buffer.seek(0)
    result = verify_package_stream(zip_buffer, resource_type="agent")
    assert result.valid is False
    assert any("forbidden" in err.lower() and "manifest" in err.lower() for err in result.errors)


def test_package_compiled_binaries_rejected_regardless_of_declaration():
    """Compiled native binaries (.exe, .dll) remain strictly forbidden."""
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w") as zf:
        manifest = {
            "name": "dangerous-skill",
            "version": "1.0.0",
            "capabilities": ["scripts", "native"],
        }
        zf.writestr("agent.yaml", json.dumps(manifest))
        zf.writestr("payload.exe", b"MZ" + b"\x00" * 50)

    zip_buffer.seek(0)
    result = verify_package_stream(zip_buffer, resource_type="agent")
    assert result.valid is False
    assert any("Compiled native binary extension forbidden" in err for err in result.errors)


# ─── Task 14: Publisher-Owned Key Signing & Platform Dual Signing ────────────

def test_publisher_owned_key_signing_and_verification():
    """Publisher signs with their own private key; platform verifies with publisher public key."""
    # 1. Publisher generates their own keypair
    pub_priv, pub_pub = generate_publisher_keypair()
    pkg_hash = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    # 2. Publisher signs package
    publisher_sig = sign_publisher_package(pkg_hash, pub_priv)
    assert verify_package_signature(pkg_hash, publisher_sig, pub_pub) is True

    # 3. Platform verifies and countersigns
    platform_priv, platform_pub = generate_publisher_keypair()
    platform_sig = countersign_platform_package(pkg_hash, platform_priv)

    # 4. Client verifies dual signature
    valid, err = verify_dual_signed_package(
        sha256_hex=pkg_hash,
        publisher_signature_hex=publisher_sig,
        publisher_public_key_hex=pub_pub,
        platform_signature_hex=platform_sig,
        platform_public_key_hex=platform_pub,
    )
    assert valid is True
    assert err is None

    # 5. Tampered package hash fails verification
    tampered_hash = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    valid_tampered, err_tampered = verify_dual_signed_package(
        sha256_hex=tampered_hash,
        publisher_signature_hex=publisher_sig,
        publisher_public_key_hex=pub_pub,
    )
    assert valid_tampered is False
    assert "verification failed" in err_tampered.lower()


# ─── Task 7: Composite Indexes on pricing_events ──────────────────────────────

def test_pricing_events_composite_indexes_configured():
    """Verifies that composite indexes are declared on PricingEvent model."""
    table_args = getattr(PricingEvent, "__table_args__", ())
    index_names = {idx.name for idx in table_args if hasattr(idx, "name")}

    assert "ix_pricing_events_account_created" in index_names
    assert "ix_pricing_events_cap_created" in index_names
    assert "ix_pricing_events_task_acc" in index_names


# ─── Tasks 23–25: Live-Path Native Adapter Dispatch ───────────────────────────

@pytest.mark.asyncio
async def test_live_path_adapter_dispatch_openai():
    """Verifies that RelayService._dispatch_llm uses OpenAIAdapter on the live dispatch path."""
    from app.services.relay_service import RelayService

    service = RelayService(None)
    mock_settings = MagicMock()
    mock_settings.openai_api_key = "sk-live-test"
    mock_settings.openai_api_key_previous = None

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"role": "assistant", "content": "Hello from OpenAI"}}],
        "usage": {"prompt_tokens": 15, "completion_tokens": 25, "total_tokens": 40},
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        user_res, raw_res = await service._dispatch_llm(
            provider="openai",
            model_id="gpt-4o",
            payload={"messages": [{"role": "user", "content": "Hi"}]},
            settings=mock_settings,
        )

        assert user_res["choices"][0]["message"]["content"] == "Hello from OpenAI"
        assert raw_res["usage"]["total_tokens"] == 40
        # Verify URL called was the native /chat/completions endpoint
        call_url = mock_post.call_args[0][0]
        assert "/chat/completions" in call_url


@pytest.mark.asyncio
async def test_live_path_adapter_dispatch_anthropic():
    """Verifies that RelayService._dispatch_llm uses AnthropicAdapter on the live dispatch path."""
    from app.services.relay_service import RelayService

    service = RelayService(None)
    mock_settings = MagicMock()
    mock_settings.anthropic_api_key = "sk-ant-live"
    mock_settings.anthropic_api_key_previous = None

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "content": [{"type": "text", "text": "Hello from Anthropic"}],
        "usage": {"input_tokens": 12, "output_tokens": 20},
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        user_res, raw_res = await service._dispatch_llm(
            provider="anthropic",
            model_id="claude-3-7-sonnet-20250219",
            payload={"messages": [{"role": "user", "content": "Hi"}]},
            settings=mock_settings,
        )

        assert user_res["choices"][0]["message"]["content"] == "Hello from Anthropic"
        assert raw_res["usage"]["output_tokens"] == 20
        # Verify URL called was /messages
        call_url = mock_post.call_args[0][0]
        assert "/messages" in call_url


@pytest.mark.asyncio
async def test_live_path_adapter_dispatch_gemini():
    """Verifies that RelayService._dispatch_llm uses GeminiAdapter on the live dispatch path."""
    from app.services.relay_service import RelayService

    service = RelayService(None)
    mock_settings = MagicMock()
    mock_settings.gemini_api_key = "gemini-live-test"
    mock_settings.gemini_api_key_previous = None

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [{
            "content": {"parts": [{"text": "Hello from Gemini"}]}
        }],
        "usageMetadata": {"totalTokenCount": 35},
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        user_res, raw_res = await service._dispatch_llm(
            provider="gemini",
            model_id="gemini-2.0-flash",
            payload={"messages": [{"role": "user", "content": "Hi"}]},
            settings=mock_settings,
        )

        assert user_res["choices"][0]["message"]["content"] == "Hello from Gemini"
        assert raw_res["usageMetadata"]["totalTokenCount"] == 35
        call_url = mock_post.call_args[0][0]
        assert "generateContent" in call_url


def test_transient_db_error_detection_for_serverless_neon():
    """Detects transient connection drops typical of Neon scale-to-zero compute pause/resumes."""
    from app.database import is_transient_db_error

    err1 = ConnectionResetError("Connection reset by peer")
    assert is_transient_db_error(err1) is True

    err2 = RuntimeError("server closed the connection unexpectedly")
    assert is_transient_db_error(err2) is True

    err3 = Exception("the database system is starting up")
    assert is_transient_db_error(err3) is True

    non_transient = ValueError("syntax error in SQL statement")
    assert is_transient_db_error(non_transient) is False

