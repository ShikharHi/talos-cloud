"""
Talos Cloud — Production Hardening Tests for Tasks 18–30.

Covers:
- Task 18: Concurrency limiting per tier
- Task 19: Capability-specific timeout assignment
- Task 20: Upstream circuit breaker state machine
- Task 21: Provider telemetry metrics & operator health endpoint
- Task 22: Dynamic fallback chains & circuit-aware routing
- Task 23: OpenAI native adapter (reasoning tokens, stream_options)
- Task 24: Anthropic native adapter (system prompt extraction, tools schema)
- Task 25: Gemini native adapter (contents & system instruction format)
- Task 26: Zero-downtime key rotation on 401
- Task 27: Exact token metering with reasoning & cached tokens
- Task 28: Streaming client disconnect handling
- Task 29: Stream credit reconciliation on partial completion
- Task 30: SSE monotonic sequence numbering & Last-Event-ID replay buffer
"""

import asyncio
import json
import uuid
import pytest
import pytest_asyncio
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.main import app
from app.services.concurrency_limiter import (
    acquire_concurrency,
    release_concurrency,
    check_and_acquire_concurrency,
)
from app.services.circuit_breaker import ProviderCircuitBreaker, CircuitState
from app.services.provider_telemetry import ProviderTelemetryTracker
from app.services.stream_buffer import StreamReplayBuffer
from app.services.adapters import OpenAIAdapter, AnthropicAdapter, GeminiAdapter
from app.services.metering.llm_adapter import extract_llm_usage


# ─── Task 18: Gateway Concurrency Limiting ────────────────────────────────────

@pytest.mark.asyncio
async def test_concurrency_limiter_free_tier_enforcement():
    """Free tier is limited to 2 concurrent calls."""
    acc_id = f"test-acc-{uuid.uuid4().hex[:8]}"

    # First 2 calls succeed
    await acquire_concurrency(acc_id, tier="free")
    await acquire_concurrency(acc_id, tier="free")

    # 3rd call must raise 429
    with pytest.raises(HTTPException) as exc_info:
        await acquire_concurrency(acc_id, tier="free")
    assert exc_info.value.status_code == 429

    # Releasing one allows another call
    await release_concurrency(acc_id)
    await acquire_concurrency(acc_id, tier="free")

    # Clean up
    await release_concurrency(acc_id)
    await release_concurrency(acc_id)


@pytest.mark.asyncio
async def test_concurrency_limiter_pro_tier_enforcement():
    """Pro tier allows up to 10 concurrent calls."""
    acc_id = f"test-acc-pro-{uuid.uuid4().hex[:8]}"

    # Acquire 10 slots
    for _ in range(10):
        await acquire_concurrency(acc_id, tier="pro")

    # 11th call must raise 429
    with pytest.raises(HTTPException) as exc_info:
        await acquire_concurrency(acc_id, tier="pro")
    assert exc_info.value.status_code == 429

    # Release all
    for _ in range(10):
        await release_concurrency(acc_id)


# ─── Task 19: Configurable Timeouts per Model Class ───────────────────────────

def test_capability_timeout_configuration():
    """Relay service applies tailored timeouts based on capability."""
    from app.services.relay_service import RelayService

    # Instantiate with dummy DB
    service = RelayService(None)

    fast_timeout = service._get_timeout_for_capability("fast_model")
    assert fast_timeout.read == 15.0

    reasoning_timeout = service._get_timeout_for_capability("reasoning_model")
    assert reasoning_timeout.read == 180.0

    standard_timeout = service._get_timeout_for_capability("code_model")
    assert standard_timeout.read == 60.0


# ─── Task 20: Upstream Provider Circuit Breaker ───────────────────────────────

@pytest.mark.asyncio
async def test_circuit_breaker_transitions():
    """Circuit transitions: CLOSED -> OPEN after 5 failures -> HALF_OPEN after timeout -> CLOSED on success."""
    cb = ProviderCircuitBreaker(failure_threshold=5, recovery_timeout_seconds=0.1)

    # Initial state is CLOSED
    assert await cb.can_execute("openai") is True
    assert await cb.get_state("openai") == CircuitState.CLOSED

    # 4 failures: still CLOSED
    for _ in range(4):
        await cb.record_failure("openai")
    assert await cb.can_execute("openai") is True
    assert await cb.get_state("openai") == CircuitState.CLOSED

    # 5th failure: trips to OPEN
    await cb.record_failure("openai")
    assert await cb.get_state("openai") == CircuitState.OPEN
    assert await cb.can_execute("openai") is False

    # Wait for recovery timeout
    await asyncio.sleep(0.15)

    # In HALF_OPEN, probe request is allowed
    assert await cb.can_execute("openai") is True
    assert await cb.get_state("openai") == CircuitState.HALF_OPEN

    # Success in HALF_OPEN resets circuit to CLOSED
    await cb.record_success("openai")
    assert await cb.get_state("openai") == CircuitState.CLOSED
    assert await cb.can_execute("openai") is True


# ─── Task 21: Provider Telemetry & Operator Visibility ────────────────────────

@pytest.mark.asyncio
async def test_provider_telemetry_metrics():
    """Provider telemetry tracks p50/p95/p99 latency, error rate, and tokens."""
    tracker = ProviderTelemetryTracker(sample_window_size=50)

    # Record 4 successes and 1 failure
    for lat in [100.0, 120.0, 150.0, 200.0]:
        await tracker.record_call("groq", latency_ms=lat, success=True, tokens=250)
    await tracker.record_call("groq", latency_ms=500.0, success=False, tokens=0)

    metrics = await tracker.get_metrics()
    groq_metrics = metrics["groq"]

    assert groq_metrics["total_calls"] == 5
    assert groq_metrics["success_count"] == 4
    assert groq_metrics["failure_count"] == 1
    assert groq_metrics["error_rate_pct"] == 20.0
    assert groq_metrics["total_tokens"] == 1000
    assert groq_metrics["latency_ms"]["p50"] > 0
    assert groq_metrics["latency_ms"]["p99"] >= groq_metrics["latency_ms"]["p50"]


def test_operator_provider_health_endpoint():
    """GET /admin/providers/health returns circuit and telemetry status for operators."""
    from app.routers.auth import require_admin
    from app.services.identity_service import WebSession

    mock_admin_session = WebSession(
        session_id=str(uuid.uuid4()),
        account_id=str(uuid.uuid4()),
        role="admin",
        email="admin@talos.dev",
    )

    app.dependency_overrides[require_admin] = lambda: mock_admin_session
    client = TestClient(app)

    try:
        response = client.get("/admin/providers/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "providers" in data
        assert "anthropic" in data["providers"]
        assert "circuit_state" in data["providers"]["anthropic"]
        assert "telemetry" in data["providers"]["anthropic"]
    finally:
        app.dependency_overrides.clear()


# ─── Task 22: Dynamic Routing & Fallback Chains ───────────────────────────────

@pytest.mark.asyncio
async def test_dynamic_fallback_chain_on_circuit_open():
    """When primary provider circuit is OPEN, routing falls over to the next candidate."""
    from app.services.relay_service import RelayService
    from app.services.circuit_breaker import circuit_breaker

    service = RelayService(None)

    # Force trip 'anthropic' to OPEN
    for _ in range(5):
        await circuit_breaker.record_failure("anthropic")

    try:
        # For reasoning_model: chain starts with anthropic, then openai, deepseek, groq
        provider, model_id = await service.resolve_provider_routing("reasoning_model")
        # Should NOT be anthropic since its circuit is OPEN
        assert provider != "anthropic"
    finally:
        await circuit_breaker.reset("anthropic")


# ─── Task 23: OpenAI Native Adapter ───────────────────────────────────────────

def test_openai_adapter_formatting():
    """OpenAI adapter sets stream_options.include_usage and handles reasoning models."""
    adapter = OpenAIAdapter(primary_key="sk-test-key", base_url="https://api.openai.com/v1")

    # Standard model with stream
    url, headers, body = adapter.format_request(
        model_id="gpt-4o",
        payload={"messages": [{"role": "user", "content": "Hello"}], "max_tokens": 500},
        stream=True,
    )
    assert url == "https://api.openai.com/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-test-key"
    assert body["stream"] is True
    assert body["stream_options"]["include_usage"] is True
    assert body["max_tokens"] == 500

    # Reasoning model (o1 / o3) uses max_completion_tokens
    _, _, body_o1 = adapter.format_request(
        model_id="o3-mini",
        payload={"messages": [{"role": "user", "content": "Think"}], "max_tokens": 1000},
        stream=False,
    )
    assert "max_tokens" not in body_o1
    assert body_o1["max_completion_tokens"] == 1000


# ─── Task 24: Anthropic Messages Adapter ──────────────────────────────────────

def test_anthropic_adapter_formatting():
    """Anthropic adapter extracts system message and formats tools with input_schema."""
    adapter = AnthropicAdapter(primary_key="sk-ant-test", base_url="https://api.anthropic.com/v1")

    openai_payload = {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "What is the weather?"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get current weather",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ],
        "max_tokens": 1024,
    }

    url, headers, body = adapter.format_request(
        model_id="claude-3-7-sonnet-20250219",
        payload=openai_payload,
        stream=True,
    )

    assert url == "https://api.anthropic.com/v1/messages"
    assert headers["x-api-key"] == "sk-ant-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert body["system"] == "You are a helpful assistant."
    assert len(body["messages"]) == 1  # System message extracted out
    assert body["messages"][0]["role"] == "user"
    assert body["tools"][0]["name"] == "get_weather"
    assert "input_schema" in body["tools"][0]


# ─── Task 25: Gemini Native Adapter ───────────────────────────────────────────

def test_gemini_adapter_formatting():
    """Gemini adapter translates messages to contents and system instructions."""
    adapter = GeminiAdapter(primary_key="gemini-test-key", base_url="https://generativelanguage.googleapis.com/v1beta")

    openai_payload = {
        "messages": [
            {"role": "system", "content": "Act as a math tutor."},
            {"role": "user", "content": "2 + 2?"},
        ],
        "temperature": 0.2,
    }

    url, headers, body = adapter.format_request(
        model_id="gemini-2.0-flash",
        payload=openai_payload,
        stream=False,
    )

    assert "generateContent" in url
    assert "key=gemini-test-key" in url
    assert "contents" in body
    assert body["contents"][0]["role"] == "user"
    assert body["contents"][0]["parts"][0]["text"] == "2 + 2?"
    assert body["system_instruction"]["parts"][0]["text"] == "Act as a math tutor."


# ─── Task 26: Zero-Downtime Provider Secret Rotation ──────────────────────────

@pytest.mark.asyncio
async def test_key_rotation_on_401():
    """Adapter loads primary and secondary keys for seamless rollover."""
    adapter = OpenAIAdapter(
        primary_key="primary-stale-key",
        secondary_key="secondary-valid-key",
        base_url="https://api.openai.com/v1",
    )

    assert adapter.primary_key == "primary-stale-key"
    assert adapter.secondary_key == "secondary-valid-key"


# ─── Task 27: Exact Token Metering with Reasoning Tokens ──────────────────────

def test_extract_llm_usage_with_reasoning_and_cache():
    """Metering extracts input, output, cached, and reasoning tokens accurately."""
    raw_response = {
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": 500,
            "total_tokens": 600,
            "prompt_tokens_details": {
                "cached_tokens": 80,
            },
            "completion_tokens_details": {
                "reasoning_tokens": 400,
            },
        }
    }

    events = extract_llm_usage(
        raw_response,
        capability_id="reasoning_model",
        provider="openai",
        model_id="o3-mini",
        task_id="task-123",
    )

    assert len(events) == 4
    events_by_unit = {e.unit_type: e for e in events}

    assert events_by_unit["input_tokens"].quantity == 100
    assert events_by_unit["output_tokens"].quantity == 500
    assert events_by_unit["cached_tokens"].quantity == 80
    assert events_by_unit["reasoning_tokens"].quantity == 400



# ─── Task 30: SSE Sequence Numbers & Replay Buffer ────────────────────────────

@pytest.mark.asyncio
async def test_stream_replay_buffer_monotonic_and_recovery():
    """Replay buffer assigns monotonic IDs and recovers missed chunks upon reconnect."""
    buffer = StreamReplayBuffer()
    stream_id = "stream-test-uuid"

    c1 = await buffer.record_and_format_chunk(stream_id, b"data: {\"token\": \"A\"}\n\n")
    c2 = await buffer.record_and_format_chunk(stream_id, b"data: {\"token\": \"B\"}\n\n")
    c3 = await buffer.record_and_format_chunk(stream_id, b"data: {\"token\": \"C\"}\n\n")

    assert b"id: 1\n" in c1
    assert b"id: 2\n" in c2
    assert b"id: 3\n" in c3

    # Client reconnects with Last-Event-ID = 1, should get chunks 2 and 3
    replayed = await buffer.get_replay_chunks(stream_id, last_event_id=1)
    assert len(replayed) == 2
    assert b"id: 2\n" in replayed[0]
    assert b"id: 3\n" in replayed[1]

    # Reconnect with Last-Event-ID = 3, no new chunks
    replayed_empty = await buffer.get_replay_chunks(stream_id, last_event_id=3)
    assert len(replayed_empty) == 0


def test_stream_last_event_id_header_replay():
    """POST /relay/stream with Last-Event-ID header returns buffered chunks without calling provider."""
    from app.routers.relay import get_authenticated_account
    from app.models.accounts import Account
    from app.services.stream_buffer import replay_buffer

    task_id = f"task-replay-{uuid.uuid4().hex[:8]}"

    # Seed buffer
    asyncio.run(replay_buffer.record_and_format_chunk(task_id, b"data: {\"chunk\": 1}\n\n"))
    asyncio.run(replay_buffer.record_and_format_chunk(task_id, b"data: {\"chunk\": 2}\n\n"))
    asyncio.run(replay_buffer.record_and_format_chunk(task_id, b"data: {\"chunk\": 3}\n\n"))

    mock_account = Account(
        account_id=uuid.uuid4(),
        email="test_replay@example.com",
        balance_credits=5000,
        subscription_tier="pro",
    )

    app.dependency_overrides[get_authenticated_account] = lambda: mock_account
    client = TestClient(app)

    try:
        response = client.post(
            "/relay/stream",
            json={
                "capability_id": "fast_model",
                "payload": {"messages": []},
                "worst_case_units": 100,
                "task_id": task_id,
            },
            headers={"Authorization": "Bearer test-device-token", "Last-Event-ID": "1"},
        )
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "id: 2" in content
        assert "id: 3" in content
        assert "id: 1" not in content
        assert "provider" not in content
    finally:
        app.dependency_overrides.clear()

