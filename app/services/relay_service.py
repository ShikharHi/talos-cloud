"""
Talos Cloud — Relay Service (Engine v3).

THIS IS THE CENTRAL EXECUTION LAYER OF THE TALOS BILLING ARCHITECTURE.

Flow:
  Client Request → Account Authentication
    ↓
  WalletEngine.reserve(account_id, task_id, worst_case_credits)
    ↓
  Provider Dispatch (keys server-side ONLY)
    ├── Success → MeteringAdapter.extract() → MeteringEvents
    │             ProviderCostCalculator → provider_cost_usd
    │             CapabilityPricingEngine → credits_charged
    │             UsageEvent DB persistence
    │             WalletEngine.commit(reservation_id, actual_credits)
    │
    └── Failure → WalletEngine.release(reservation_id)  [charge = 0]

CRITICAL INVARIANTS:
  1. All pre-checks and balance deductions go through WalletEngine.
  2. `provider` and `model_id` are NEVER included in any HTTP response body
     returned to a client. They stay in UsageEvent/PricingEvent rows for internal use.
  3. Real provider API keys are used HERE AND ONLY HERE.
  4. Streaming: httpx AsyncClient with stream(). Reconciled after stream ends.
  5. Idempotent: idempotency_key prevents double-reservation on retry.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.accounts import Account
from app.models.ledger import PricingEvent
from app.models.usage_event import UsageEvent, UnitType
from app.models.wallet import CreditReservation
from app.services.wallet_engine import InsufficientCreditsError, WalletEngine
from app.services.pricing_calculator import CapabilityPricingEngine, ProviderCostCalculator
from app.services.metering import (
    extract_browser_usage,
    extract_image_usage,
    extract_llm_usage,
    extract_search_usage,
)
from app.services.metering.base import MeteringEvent
from app.services.model_registry import ModelRegistry

logger = logging.getLogger(__name__)


class RelayService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.settings = get_settings()
        self.wallet_engine = WalletEngine(db)
        self.cost_calculator = ProviderCostCalculator(db)
        self.pricing_engine = CapabilityPricingEngine(db)
        self.model_registry = ModelRegistry(db)

    async def call(
        self,
        account_id: uuid.UUID,
        task_id: str | None,
        capability_id: str,
        payload: dict,
        worst_case_units: int,
        idempotency_key: str | None = None,
    ) -> dict:
        """
        Main relay entry point for non-streaming calls.
        Returns dict: {"result": ..., "credits_charged": ..., "capability_id": ...}

        INVARIANT: 'provider' and 'model_id' are NEVER in the return dictionary.
        """
        acc_uuid = account_id if isinstance(account_id, uuid.UUID) else uuid.UUID(str(account_id))

        # Check if user is admin / unlimited tier
        is_unlimited = await self._is_unlimited_account(acc_uuid)

        # Estimate worst-case credits to hold
        worst_case_event = MeteringEvent(
            capability_id=capability_id,
            provider="precheck",
            model_id=None,
            unit_type=self._capability_to_default_unit(capability_id),
            quantity=worst_case_units,
        )
        worst_case_cost = await self.cost_calculator.calculate_cost(worst_case_event)
        credits_to_hold = await self.pricing_engine.credits_for_event(worst_case_event, worst_case_cost)
        credits_to_hold = max(1, credits_to_hold)

        reservation: CreditReservation | None = None

        # ── Step 1: Pre-check & Reserve Credits ─────────────────────────────
        if not is_unlimited:
            try:
                reservation = await self.wallet_engine.reserve(
                    account_id=acc_uuid,
                    task_id=task_id,
                    amount=credits_to_hold,
                    idempotency_key=idempotency_key,
                )
            except InsufficientCreditsError as e:
                # Log rejected event before raising
                await self._log_rejected_event(
                    account_id=acc_uuid,
                    task_id=task_id,
                    capability_id=capability_id,
                    worst_case_units=worst_case_units,
                )
                raise e

        # ── Step 2: Resolve Provider Routing & Dispatch ──────────────────────
        candidates: list[tuple[str, str]] = []
        try:
            p_primary, m_primary = await self.resolve_provider_routing(capability_id)
            candidates.append((p_primary, m_primary))
        except Exception:
            pass

        for p_fb, m_fb in DEFAULT_FALLBACK_CHAINS.get(capability_id, []):
            if (p_fb, m_fb) not in candidates and self._has_credentials(p_fb):
                candidates.append((p_fb, m_fb))

        if not candidates:
            candidates.append((self._fallback_provider(capability_id), self._fallback_model_id(capability_id)))

        provider_result = None
        raw_response = None
        last_dispatch_err = None
        provider = candidates[0][0]
        model_id = candidates[0][1]

        for cand_provider, cand_model in candidates:
            max_attempts = 2 if len(candidates) == 1 else 1
            for cand_attempt in range(max_attempts):
                try:
                    try:
                        provider_result, raw_response = await self._dispatch(
                            capability_id,
                            payload,
                            provider=cand_provider,
                            model_id=cand_model,
                        )
                    except TypeError:
                        provider_result, raw_response = await self._dispatch(
                            capability_id,
                            payload,
                        )
                    provider = cand_provider
                    model_id = cand_model
                    last_dispatch_err = None
                    break
                except Exception as e:
                    last_dispatch_err = e
                    err_msg = str(e).lower()
                    logger.warning(
                        "Provider '%s' dispatch failed for capability '%s' (attempt %s/%s): %s",
                        cand_provider,
                        capability_id,
                        cand_attempt + 1,
                        max_attempts,
                        e,
                    )
                    if ("429" in err_msg or "503" in err_msg or "overloaded" in err_msg) and cand_attempt < max_attempts - 1:
                        await asyncio.sleep(2.0)
                        continue
                    try:
                        from app.services.circuit_breaker import circuit_breaker
                        await circuit_breaker.record_failure(cand_provider, 429 if "429" in str(e) else 500)
                    except Exception:
                        pass
                    break

            if provider_result is not None:
                break

        if provider_result is None:
            if reservation is not None:
                await self.wallet_engine.release(reservation.reservation_id)
            err_str = str(last_dispatch_err or "")
            if "429" in err_str or "too many requests" in err_str.lower():
                from fastapi import HTTPException
                raise HTTPException(
                    status_code=429,
                    detail="The AI service is currently busy or rate-limited. Please retry in a moment.",
                )
            raise RuntimeError(f"Provider call failed for capability '{capability_id}': {last_dispatch_err}") from last_dispatch_err

        if raw_response is None:
            raise RuntimeError(f"Provider returned no raw response for capability '{capability_id}'")

        # ── Step 3: Metering & Credit Calculation ───────────────────────────
        metering_events = self._extract_metering_events(
            capability_id=capability_id,
            provider=provider,
            model_id=model_id,
            payload=payload,
            raw_response=raw_response,
            task_id=task_id,
        )

        total_actual_credits = 0
        pricing_version = await self.pricing_engine.get_active_pricing_version()

        for event in metering_events:
            # Calculate real USD cost
            cost_usd = await self.cost_calculator.calculate_cost(event)
            event.provider_cost_usd = cost_usd

            # Calculate credits to charge
            if is_unlimited:
                credits_charged = 0
            else:
                credits_charged = await self.pricing_engine.credits_for_event(event, cost_usd)

            event.credits_charged = credits_charged
            event.pricing_version = pricing_version
            total_actual_credits += credits_charged

            # Persist UsageEvent DB row (INTERNAL ONLY)
            await self._persist_usage_event(acc_uuid, event)

        # ── Step 4: Commit Reservation ───────────────────────────────────────
        if reservation is not None and not is_unlimited:
            await self.wallet_engine.commit(
                reservation_id=reservation.reservation_id,
                actual_amount=total_actual_credits,
            )

        # Write PricingEvent row for backward compatibility
        await self._log_pricing_event(
            account_id=acc_uuid,
            task_id=task_id,
            capability_id=capability_id,
            provider=provider,
            actual_units=sum(e.quantity for e in metering_events),
            precheck_units=worst_case_units,
            rejected=False,
            credits_charged=0 if is_unlimited else total_actual_credits,
            pricing_version=pricing_version,
        )

        # INVARIANT CHECK: provider and model_id must NOT be in the return value
        return {
            "result": provider_result,
            "credits_charged": 0 if is_unlimited else total_actual_credits,
            "capability_id": capability_id,
        }

    async def stream_call(
        self,
        account_id: uuid.UUID,
        task_id: str | None,
        capability_id: str,
        payload: dict,
        worst_case_units: int,
        idempotency_key: str | None = None,
        request: Any = None,
    ) -> AsyncIterator[bytes]:
        """
        Streaming relay call with:
        - Canonical Incremental SSE parsing (reconstructing complete events across TCP chunk boundaries)
        - Provider normalization adapter producing Talos native stream events
        - Strict StreamStateMachine guaranteeing exactly ONE terminal state (COMPLETED, FAILED, CANCELLED)
        - Immediate delta forwarding (no blocking replay/database in live path)
        - Out-of-band non-blocking event replay recording
        - Cancellation-safe credit reconciliation (no orphaned reservations)
        """
        import time
        from app.services.stream_parser import IncrementalSSEParser
        from app.services.stream_adapter import ProviderStreamAdapter, StreamState, StreamStateMachine
        from app.services.stream_buffer import replay_buffer

        acc_uuid = account_id if isinstance(account_id, uuid.UUID) else uuid.UUID(str(account_id))
        is_unlimited = await self._is_unlimited_account(acc_uuid)

        worst_case_event = MeteringEvent(
            capability_id=capability_id,
            provider="precheck",
            model_id=None,
            unit_type="input_tokens",
            quantity=worst_case_units,
        )
        worst_case_cost = await self.cost_calculator.calculate_cost(worst_case_event)
        credits_to_hold = await self.pricing_engine.credits_for_event(worst_case_event, worst_case_cost)
        credits_to_hold = max(1, credits_to_hold)

        reservation: CreditReservation | None = None
        if not is_unlimited:
            reservation = await self.wallet_engine.reserve(
                account_id=acc_uuid,
                task_id=task_id,
                amount=credits_to_hold,
                idempotency_key=idempotency_key,
            )

        candidates: list[tuple[str, str]] = []
        try:
            p_primary, m_primary = await self.resolve_provider_routing(capability_id)
            candidates.append((p_primary, m_primary))
        except Exception:
            pass

        for p_fb, m_fb in DEFAULT_FALLBACK_CHAINS.get(capability_id, []):
            if (p_fb, m_fb) not in candidates and self._has_credentials(p_fb):
                candidates.append((p_fb, m_fb))

        if not candidates:
            candidates.append((self._fallback_provider(capability_id), self._fallback_model_id(capability_id)))

        provider = candidates[0][0]
        model_id = candidates[0][1]

        stream_id = task_id or str(uuid.uuid4())
        sm = StreamStateMachine(stream_id)
        adapter = ProviderStreamAdapter(provider, sm)
        parser = IncrementalSSEParser()

        # Emit stream.start immediately to inform downstream client
        start_evt = sm.transition_started()
        seq = 1
        start_bytes = start_evt.to_sse_bytes(event_id=seq)
        replay_buffer.record_event_background(stream_id, start_bytes, seq)
        yield start_bytes

        stream_timeout = self._get_timeout_for_capability(capability_id)
        disconnected = False
        t_req_start = time.perf_counter()
        first_delta_sent = False

        response = None
        active_client = None
        last_resp_code = 500

        for cand_provider, cand_model in candidates:
            provider = cand_provider
            model_id = cand_model
            adapter = ProviderStreamAdapter(provider, sm)
            max_attempts = 2 if len(candidates) == 1 else 1
            for cand_attempt in range(max_attempts):
                try:
                    url, headers, body = await self._build_provider_request(provider, model_id, payload)
                    client = httpx.AsyncClient(timeout=stream_timeout)
                    resp = await client.send(client.build_request("POST", url, headers=headers, json=body), stream=True)
                    if resp.status_code == 200:
                        response = resp
                        active_client = client
                        break
                    else:
                        last_resp_code = resp.status_code
                        err_bytes = await resp.aread()
                        await resp.aclose()
                        await client.aclose()
                        logger.warning(
                            "Provider '%s' returned HTTP %s for stream (attempt %s/%s): %s",
                            provider, resp.status_code, cand_attempt + 1, max_attempts, err_bytes[:200]
                        )
                        if resp.status_code in (429, 503) and cand_attempt < max_attempts - 1:
                            await asyncio.sleep(2.0)
                            continue
                        break
                except Exception as e:
                    logger.warning("Provider '%s' connection failed for stream: %s. Trying fallback candidate...", provider, e)
                    break

            if response is not None:
                break

        try:
            if response is None:
                if last_resp_code == 429:
                    clean_msg = "The model service is temporarily busy. Please try again in a moment."
                    err_code = "rate_limit_exceeded"
                else:
                    clean_msg = "Model generation failed. Please try again."
                    err_code = "upstream_error"

                fail_evt = sm.transition_failed(clean_msg, code=err_code)
                if fail_evt:
                    seq += 1
                    fail_bytes = fail_evt.to_sse_bytes(event_id=seq)
                    replay_buffer.record_event_background(stream_id, fail_bytes, seq)
                    yield fail_bytes
            else:
                async for raw_chunk in response.aiter_bytes():
                    # Client disconnect detection
                    if request and await request.is_disconnected():
                        logger.info("Client disconnected during stream for task '%s'. Cancelling upstream stream.", task_id)
                        disconnected = True
                        canc_evt = sm.transition_cancelled()
                        if canc_evt:
                            seq += 1
                            canc_bytes = canc_evt.to_sse_bytes(event_id=seq)
                            replay_buffer.record_event_background(stream_id, canc_bytes, seq)
                            yield canc_bytes
                        break

                    # Feed raw bytes incrementally to reconstruct complete SSE messages
                    for sse_msg in parser.feed(raw_chunk):
                        for talos_evt in adapter.process_message(sse_msg):
                            if not first_delta_sent and talos_evt.type == "stream.delta":
                                first_delta_sent = True
                                ttft_ms = (time.perf_counter() - t_req_start) * 1000
                                talos_evt.metadata["ttft_ms"] = round(ttft_ms, 2)

                            seq += 1
                            evt_bytes = talos_evt.to_sse_bytes(event_id=seq)
                            replay_buffer.record_event_background(stream_id, evt_bytes, seq)
                            yield evt_bytes

                # Flush remaining parsed messages
                for sse_msg in parser.flush():
                    for talos_evt in adapter.process_message(sse_msg):
                        seq += 1
                        evt_bytes = talos_evt.to_sse_bytes(event_id=seq)
                        replay_buffer.record_event_background(stream_id, evt_bytes, seq)
                        yield evt_bytes

        except (httpx.RemoteProtocolError, httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            if not sm.is_terminal:
                fail_evt = sm.transition_failed(str(e), code="provider_connection_error")
                if fail_evt:
                    seq += 1
                    fail_bytes = fail_evt.to_sse_bytes(event_id=seq)
                    replay_buffer.record_event_background(stream_id, fail_bytes, seq)
                    yield fail_bytes
        except Exception as e:
            if not sm.is_terminal:
                fail_evt = sm.transition_failed(str(e), code="internal_stream_error")
                if fail_evt:
                    seq += 1
                    fail_bytes = fail_evt.to_sse_bytes(event_id=seq)
                    replay_buffer.record_event_background(stream_id, fail_bytes, seq)
                    yield fail_bytes
        finally:
            if response is not None:
                try:
                    await response.aclose()
                except Exception:
                    pass
            if active_client is not None:
                try:
                    await active_client.aclose()
                except Exception:
                    pass

            # Guarantee exactly ONE terminal event
            if not sm.is_terminal:
                if disconnected:
                    canc_evt = sm.transition_cancelled()
                    if canc_evt:
                        seq += 1
                        canc_bytes = canc_evt.to_sse_bytes(event_id=seq)
                        replay_buffer.record_event_background(stream_id, canc_bytes, seq)
                        yield canc_bytes
                else:
                    comp_evt = sm.transition_completed(finish_reason="stop")
                    if comp_evt:
                        seq += 1
                        comp_bytes = comp_evt.to_sse_bytes(event_id=seq)
                        replay_buffer.record_event_background(stream_id, comp_bytes, seq)
                        yield comp_bytes

        # Reconcile billing atomically:
        provider_actual_units = sm.exact_tokens if sm.exact_tokens > 0 else max(1, sm.deltas_count)
        billing_policy = getattr(self.settings, "stream_billing_policy", "charge_actual")
        if billing_policy == "charge_delivered" and disconnected:
            billed_units = min(provider_actual_units, max(1, sm.deltas_count))
        else:
            billed_units = provider_actual_units

        try:
            stream_event = MeteringEvent(
                capability_id=capability_id,
                provider=provider,
                model_id=model_id,
                unit_type="output_tokens",
                quantity=billed_units,
                task_id=task_id,
            )
            cost_usd = await self.cost_calculator.calculate_cost(stream_event)
            actual_credits = 0 if is_unlimited else await self.pricing_engine.credits_for_event(stream_event, cost_usd)

            stream_event.provider_cost_usd = cost_usd
            stream_event.credits_charged = actual_credits
            stream_event.metadata = {
                "terminal_state": sm.current_state.value,
                "deltas_count": sm.deltas_count,
                "interrupted": disconnected,
                "billing_policy": billing_policy,
            }
            await self._persist_usage_event(acc_uuid, stream_event)

            if reservation is not None and not is_unlimited:
                if sm.current_state == StreamState.FAILED and sm.deltas_count == 0:
                    # If total failure before any output, release reservation in full
                    await self.wallet_engine.release(reservation.reservation_id)
                else:
                    await self.wallet_engine.commit(reservation.reservation_id, actual_credits)

            await self._log_pricing_event(
                account_id=acc_uuid,
                task_id=task_id,
                capability_id=capability_id,
                provider=provider,
                actual_units=billed_units,
                precheck_units=worst_case_units,
                rejected=False,
                credits_charged=actual_credits,
                pricing_version=await self.pricing_engine.get_active_pricing_version(),
            )
        except Exception as e:
            logger.warning("Error recording stream usage reconciliation: %s", e)
            if reservation is not None and not is_unlimited:
                try:
                    await self.wallet_engine.release(reservation.reservation_id)
                except Exception:
                    pass


    # ─── Internal Dispatch & Metering Helpers ─────────────────────────────────

    async def _is_unlimited_account(self, account_id: uuid.UUID) -> bool:
        result = await self.db.execute(
            select(Account).where(Account.account_id == account_id)
        )
        acc = result.scalar_one_or_none()
        if acc is None:
            return False
        return bool(
            acc.role == "admin"
            or acc.subscription_tier in ("admin", "unlimited")
            or (acc.email and acc.email.lower() in [e.lower() for e in self.settings.admin_email_list])
        )

    async def _dispatch(
        self, capability_id: str, payload: dict, provider: str = "zhipu", model_id: str = "glm-4.5-flash"
    ) -> tuple[dict, dict]:
        """
        Dispatches request to provider using server-side API keys.
        Returns (user_visible_result_dict, raw_provider_response_dict).
        """
        settings = self.settings

        if capability_id in ("reasoning_model", "code_model", "fast_model", "vision_model"):
            return await self._dispatch_llm(provider, model_id, payload, settings)
        elif capability_id == "web_search":
            return await self._dispatch_search(payload, settings)
        elif capability_id == "image_gen":
            return await self._dispatch_image(payload, settings)
        elif capability_id == "browser_use":
            return {"status": "success", "seconds": payload.get("seconds", 60)}, {"browser_seconds": payload.get("seconds", 60)}
        else:
            raise ValueError(f"Unknown capability_id: {capability_id}")

    async def _dispatch_image(self, payload: dict, settings) -> tuple[dict, dict]:
        """Dispatch an image-generation request through the configured provider."""
        provider = payload.get("provider", "openai").lower().strip()
        api_key, _, base_url = self._get_provider_credentials(provider, settings)
        if not api_key:
            raise RuntimeError(f"API key not configured for provider '{provider}'")

        body = {
            "model": payload.get("model", "dall-e-3"),
            "prompt": payload.get("prompt", ""),
            "n": payload.get("n", 1),
            "size": payload.get("size", "1024x1024"),
        }
        for key in ("quality", "style", "response_format"):
            if key in payload:
                body[key] = payload[key]

        timeout = self._get_timeout_for_capability("image_gen")
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url}/images/generations",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
            response.raise_for_status()
            data = response.json()

        return {"data": data.get("data", [])}, data

    async def _dispatch_search(self, payload: dict, settings) -> tuple[dict, dict]:
        """Dispatch a web-search request through Tavily."""
        api_key = self._normalize_key(getattr(settings, "tavily_api_key", None))
        if not api_key:
            raise RuntimeError("API key not configured for provider 'tavily'")

        body = {
            "query": payload.get("query", payload.get("q", "")),
            "search_depth": payload.get("search_depth", "basic"),
            "max_results": payload.get("max_results", 5),
            "include_answer": payload.get("include_answer", False),
        }
        for key in ("include_raw_content", "include_images", "topic", "time_range"):
            if key in payload:
                body[key] = payload[key]

        timeout = self._get_timeout_for_capability("web_search")
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                "https://api.tavily.com/search",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=body,
            )
            response.raise_for_status()
            data = response.json()

        return {"results": data.get("results", []), "answer": data.get("answer")}, data

    def _get_timeout_for_capability(self, capability_id: str) -> httpx.Timeout:
        """Configurable timeouts per model class (Task 19)."""
        if capability_id in ("fast_model", "web_search"):
            return httpx.Timeout(connect=15.0, read=90.0, write=30.0, pool=30.0)
        elif capability_id == "reasoning_model":
            return httpx.Timeout(connect=15.0, read=300.0, write=60.0, pool=60.0)
        else:
            return httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=30.0)

    @staticmethod
    def _normalize_key(value: str | None) -> str:
        return (value or "").strip()

    def _get_provider_credentials(self, provider: str, settings) -> tuple[str, str | None, str]:
        """Returns (primary_key, secondary_key, base_url)."""
        p = provider.lower().strip()
        if p == "anthropic":
            return self._normalize_key(settings.anthropic_api_key), self._normalize_key(settings.anthropic_api_key_previous) or None, "https://api.anthropic.com/v1"
        elif p == "openai":
            return self._normalize_key(settings.openai_api_key), self._normalize_key(settings.openai_api_key_previous) or None, "https://api.openai.com/v1"
        elif p == "gemini":
            return self._normalize_key(settings.gemini_api_key), self._normalize_key(settings.gemini_api_key_previous) or None, "https://generativelanguage.googleapis.com/v1beta"
        elif p == "groq":
            return self._normalize_key(settings.groq_api_key), self._normalize_key(settings.groq_api_key_previous) or None, "https://api.groq.com/openai/v1"
        elif p == "deepseek":
            key = self._normalize_key(settings.deepseek_api_key) or self._normalize_key(settings.groq_api_key)
            return key, self._normalize_key(settings.deepseek_api_key_previous) or None, "https://api.deepseek.com/v1"
        elif p in ("zhipu", "z.ai"):
            key = self._normalize_key(settings.zai_api_key) or self._normalize_key(settings.zhipu_api_key) or self._normalize_key(os.environ.get("ZAI_API_KEY"))
            return key, self._normalize_key(settings.zai_api_key_previous) or None, "https://api.z.ai/api/paas/v4"
        else:
            key = self._normalize_key(settings.zai_api_key) or self._normalize_key(settings.zhipu_api_key) or self._normalize_key(os.environ.get("ZAI_API_KEY"))
            return key, None, "https://api.z.ai/api/paas/v4"

    def _has_credentials(self, provider: str) -> bool:
        key, _, _ = self._get_provider_credentials(provider, self.settings)
        return bool(key)

    async def _dispatch_llm(
        self, provider: str, model_id: str, payload: dict, settings
    ) -> tuple[dict, dict]:
        """Executes LLM call using server-side key, circuit breaker, and zero-downtime rotation."""
        import time
        from app.services.circuit_breaker import circuit_breaker
        from app.services.provider_telemetry import telemetry_tracker

        primary_key, secondary_key, base_url = self._get_provider_credentials(provider, settings)
        primary_key = self._normalize_key(primary_key)
        secondary_key = self._normalize_key(secondary_key) if secondary_key else None
        if not primary_key:
            raise RuntimeError(f"API key not configured for provider '{provider}'")

        timeout = self._get_timeout_for_capability(payload.get("capability_id", "default"))
        t0 = time.perf_counter()
        p = provider.lower().strip()

        try:
            if p == "anthropic":
                from app.services.adapters import AnthropicAdapter
                adapter = AnthropicAdapter(primary_key, secondary_key, base_url)
                url, headers, body = adapter.format_request(model_id, payload, stream=False)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, headers=headers, json=body)
                    if resp.status_code == 401 and secondary_key:
                        logger.warning("Anthropic primary key failed with 401; attempting secondary key rotation")
                        headers = adapter.get_rotated_headers(use_secondary=True)
                        resp = await client.post(url, headers=headers, json=body)
                    resp.raise_for_status()
                    data = resp.json()
                content_text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
                user_result = {"choices": [{"message": {"role": "assistant", "content": content_text}}]}
                raw_response = data

            elif p == "gemini":
                from app.services.adapters import GeminiAdapter
                adapter = GeminiAdapter(primary_key, secondary_key, base_url)
                url, headers, body = adapter.format_request(model_id, payload, stream=False)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, headers=headers, json=body)
                    if resp.status_code == 401 and secondary_key:
                        logger.warning("Gemini primary key failed with 401; attempting secondary key rotation")
                        url = adapter.get_rotated_url(model_id, stream=False, use_secondary=True)
                        resp = await client.post(url, headers=headers, json=body)
                    resp.raise_for_status()
                    data = resp.json()
                candidates = data.get("candidates", [])
                text_part = ""
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    text_part = "".join(part.get("text", "") for part in parts if "text" in part)
                user_result = {"choices": [{"message": {"role": "assistant", "content": text_part}}]}
                raw_response = data

            elif p == "openai":
                from app.services.adapters import OpenAIAdapter
                adapter = OpenAIAdapter(primary_key, secondary_key, base_url)
                url, headers, body = adapter.format_request(model_id, payload, stream=False)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(url, headers=headers, json=body)
                    if resp.status_code == 401 and secondary_key:
                        logger.warning("OpenAI primary key failed with 401; attempting secondary key rotation")
                        headers = adapter.get_rotated_headers(use_secondary=True)
                        resp = await client.post(url, headers=headers, json=body)
                    resp.raise_for_status()
                    data = resp.json()
                user_result = {"choices": data.get("choices", [])}
                raw_response = data

            else:
                # Default OpenAI-compatible endpoint (groq, deepseek, zhipu)
                messages = payload.get("messages", [])
                extra_body = {k: v for k, v in payload.items() if k not in ("model", "messages")}
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {primary_key}", "Content-Type": "application/json"},
                        json={"model": model_id, "messages": messages, **extra_body},
                    )
                    if resp.status_code == 401 and secondary_key:
                        logger.warning("%s primary key failed with 401; attempting secondary key rotation", provider)
                        resp = await client.post(
                            f"{base_url}/chat/completions",
                            headers={"Authorization": f"Bearer {secondary_key}", "Content-Type": "application/json"},
                            json={"model": model_id, "messages": messages, **extra_body},
                        )
                    resp.raise_for_status()
                    data = resp.json()
                user_result = {"choices": data.get("choices", [])}
                raw_response = data

            latency_ms = (time.perf_counter() - t0) * 1000
            await circuit_breaker.record_success(provider)
            usage = raw_response.get("usage", {})
            tokens = usage.get("total_tokens") or (usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
            await telemetry_tracker.record_call(provider, latency_ms, success=True, tokens=tokens)
            return user_result, raw_response

        except Exception as e:
            latency_ms = (time.perf_counter() - t0) * 1000
            await circuit_breaker.record_failure(provider, e)
            await telemetry_tracker.record_call(provider, latency_ms, success=False, tokens=0)
            raise

    async def _build_provider_request(
        self, provider: str, model_id: str, payload: dict
    ) -> tuple[str, dict, dict]:
        p = provider.lower().strip()
        primary_key, secondary_key, base_url = self._get_provider_credentials(provider, self.settings)
        primary_key = self._normalize_key(primary_key)
        secondary_key = self._normalize_key(secondary_key) if secondary_key else None
        if not primary_key:
            raise RuntimeError(f"API key not configured for provider '{provider}'")

        if p == "anthropic":
            from app.services.adapters import AnthropicAdapter
            adapter = AnthropicAdapter(primary_key, secondary_key, base_url)
            return adapter.format_request(model_id, payload, stream=True)
        elif p == "gemini":
            from app.services.adapters import GeminiAdapter
            adapter = GeminiAdapter(primary_key, secondary_key, base_url)
            return adapter.format_request(model_id, payload, stream=True)
        elif p == "openai":
            from app.services.adapters import OpenAIAdapter
            adapter = OpenAIAdapter(primary_key, secondary_key, base_url)
            return adapter.format_request(model_id, payload, stream=True)
        else:
            body = {**payload, "model": model_id, "stream": True}
            headers = {"Authorization": f"Bearer {primary_key}", "Content-Type": "application/json"}
            return f"{base_url}/chat/completions", headers, body

    def _extract_metering_events(
        self,
        capability_id: str,
        provider: str,
        model_id: str,
        payload: dict,
        raw_response: dict,
        task_id: str | None,
    ) -> list[MeteringEvent]:
        if capability_id in ("reasoning_model", "code_model", "fast_model", "vision_model"):
            return extract_llm_usage(raw_response, capability_id, provider, model_id, task_id)
        elif capability_id == "web_search":
            return extract_search_usage(raw_response, capability_id, provider, model_id, task_id)
        elif capability_id == "browser_use":
            return extract_browser_usage(raw_response.get("browser_seconds", 60), capability_id, provider, model_id, task_id)
        elif capability_id == "image_gen":
            return extract_image_usage(raw_response, capability_id, provider, model_id, task_id)
        else:
            return [MeteringEvent(
                capability_id=capability_id,
                provider=provider,
                model_id=model_id,
                unit_type="per_call",
                quantity=1,
                task_id=task_id,
            )]

    async def _persist_usage_event(self, account_id: uuid.UUID, event: MeteringEvent) -> None:
        """Persists internal UsageEvent row to DB."""
        # Convert string unit_type to UnitType enum safely
        try:
            enum_unit = UnitType(event.unit_type)
        except ValueError:
            enum_unit = UnitType.PER_CALL

        row = UsageEvent(
            account_id=account_id,
            task_id=event.task_id,
            capability_id=event.capability_id,
            provider=event.provider,          # INTERNAL ONLY
            model_id=event.model_id,          # INTERNAL ONLY
            unit_type=enum_unit,
            quantity=event.quantity,
            provider_cost_usd=event.provider_cost_usd,
            credits_charged=event.credits_charged,
            event_metadata=event.metadata,
            pricing_version=event.pricing_version,
        )
        self.db.add(row)

    async def _log_pricing_event(
        self,
        account_id: uuid.UUID,
        task_id: str | None,
        capability_id: str,
        provider: str,
        actual_units: int,
        precheck_units: int,
        rejected: bool,
        credits_charged: int,
        pricing_version: str = "v1",
    ) -> None:
        pe = PricingEvent(
            account_id=account_id,
            task_id=task_id,
            capability_id=capability_id,
            provider=provider,  # INTERNAL ONLY
            actual_units=actual_units,
            precheck_units=precheck_units,
            rejected=rejected,
            credits_charged=credits_charged,
            pricing_version=pricing_version,
        )
        self.db.add(pe)

    async def _log_rejected_event(
        self, account_id: uuid.UUID, task_id: str | None, capability_id: str, worst_case_units: int
    ) -> None:
        pe = PricingEvent(
            account_id=account_id,
            task_id=task_id,
            capability_id=capability_id,
            provider=self._fallback_provider(capability_id),
            actual_units=0,
            precheck_units=worst_case_units,
            rejected=True,
            credits_charged=0,
            pricing_version="v1",
        )
        self.db.add(pe)

    @staticmethod
    def _capability_to_default_unit(capability_id: str) -> str:
        if capability_id in ("reasoning_model", "code_model", "fast_model", "vision_model"):
            return "output_tokens"
        elif capability_id == "image_gen":
            return "image_medium"
        else:
            return "per_call"

    @classmethod
    def _fallback_provider(cls, capability_id: str) -> str:
        chain = DEFAULT_FALLBACK_CHAINS.get(capability_id)
        if chain:
            return chain[0][0]
        return "zhipu"

    @classmethod
    def _fallback_model_id(cls, capability_id: str) -> str:
        chain = DEFAULT_FALLBACK_CHAINS.get(capability_id)
        if chain:
            return chain[0][1]
        return "default"

    async def resolve_provider_routing(self, capability_id: str) -> tuple[str, str]:
        """
        Dynamically resolves provider and model_id using model registry,
        circuit breaker state, and configured fallback chains.
        """
        from app.services.circuit_breaker import circuit_breaker

        # 1. Try model registry first
        try:
            model_config = await self.model_registry.switch_model(capability_id)
            p = model_config.provider
            if await circuit_breaker.can_execute(p) and self._has_credentials(p):
                return p, model_config.model_id
        except Exception:
            pass

        # 2. Iterate capability fallback chain
        chain = DEFAULT_FALLBACK_CHAINS.get(capability_id, [("zhipu", "glm-4.5-flash")])
        for p, m in chain:
            if await circuit_breaker.can_execute(p) and self._has_credentials(p):
                return p, m

        # 3. If all circuit breaker checks failed or keys missing, return top candidate
        return chain[0]


DEFAULT_FALLBACK_CHAINS: dict[str, list[tuple[str, str]]] = {
    "reasoning_model": [
        ("zhipu", "glm-4.5-flash"),
        ("anthropic", "claude-3-7-sonnet-20250219"),
        ("openai", "o3-mini"),
        ("deepseek", "deepseek-reasoner"),
    ],
    "fast_model": [
        ("zhipu", "glm-4.5-flash"),
        ("openai", "gpt-4o-mini"),
        ("gemini", "gemini-2.0-flash"),
    ],
    "code_model": [
        ("anthropic", "claude-3-5-sonnet-20241022"),
        ("openai", "gpt-4o"),
        ("deepseek", "deepseek-coder"),
        ("zhipu", "glm-4.5-flash"),
    ],
    "vision_model": [
        ("openai", "gpt-4o"),
        ("anthropic", "claude-3-5-sonnet-20241022"),
        ("gemini", "gemini-2.0-flash"),
        ("zhipu", "glm-4.6v-flash"),
    ],
    "web_search": [
        ("tavily", "tavily-search"),
    ],
    "browser_use": [
        ("internal", "talos-browser-runner"),
    ],
    "image_gen": [
        ("openai", "dall-e-3"),
        ("zhipu", "cogview-4-250304"),
    ],
}
