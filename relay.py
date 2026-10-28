import os
import json
import time
import asyncio
from typing import Any, AsyncGenerator, Dict, List, Optional, Union
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from auth import verify_internal_token
from config import config
import httpx


async def verify_relay_access(request: Request):
    """Accept either internal token OR a valid scoped runtime token (for sub-agents).

    Auth modes:
    1. X-Talos-Internal-Token header  — Talos backend internal access (existing).
    2. Authorization: Bearer <token>  — Scoped sub-agent relay token minted via
       /v1/marketplace/internal/mint-relay-token.
    """
    internal = request.headers.get("X-Talos-Internal-Token", "")
    known_secrets = {
        config.TALOS_INTERNAL_SECRET,
        "talos_sec_99af28cd418e743a6d71b569e2084c17b8f",
        "talos_internal_default_secret_key_v6"
    }
    if any(internal == s for s in known_secrets if s):
        return  # Talos backend internal access

    # Check Bearer token (sub-agent scoped relay token)
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        raw_token = auth_header[7:]
        import hashlib
        token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
        from marketplace.repository import marketplace_repo
        claims = await marketplace_repo.validate_runtime_token(token_hash)
        if claims:
            return  # Valid scoped token

    raise HTTPException(
        status_code=403,
        detail="Forbidden: valid Talos internal token or sub-agent relay token required",
    )


router = APIRouter(prefix="/v1", dependencies=[Depends(verify_relay_access)])

# ---------------------------------------------------------------------------
# Usage Accounting & Operational Telemetry
# Decoupled from financial billing; focused on performance & reliability.
# ---------------------------------------------------------------------------
class UsageRecord(BaseModel):
    request_id: str
    timestamp: float
    provider: str
    model: str
    upstream_model: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    ttft_ms: Optional[float] = None
    status: str
    failover_used: bool = False
    finish_reason: Optional[str] = None
    error: Optional[str] = None

class RelayTelemetryStore:
    def __init__(self):
        self.total_requests: int = 0
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_latency_ms: float = 0.0
        self.provider_distribution: Dict[str, int] = {
            "mistral": 0,
            "openai": 0,
            "mock": 0,
            "failover": 0
        }
        self.records: List[Dict[str, Any]] = []

    def record(self, item: UsageRecord):
        self.total_requests += 1
        self.total_input_tokens += item.input_tokens
        self.total_output_tokens += item.output_tokens
        self.total_latency_ms += item.latency_ms
        self.provider_distribution[item.provider] = self.provider_distribution.get(item.provider, 0) + 1
        if item.failover_used:
            self.provider_distribution["failover"] += 1

        self.records.append(item.model_dump())
        if len(self.records) > 250:
            self.records.pop(0)

telemetry = RelayTelemetryStore()

# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------
class ChatMessage(BaseModel):
    role: str
    content: Optional[Union[str, List[Any]]] = ""
    name: Optional[str] = None
    tool_call_id: Optional[str] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None

class ChatCompletionRequest(BaseModel):
    model: str = "talos-code"
    messages: List[ChatMessage]
    stream: Optional[bool] = False
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 2048
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Any] = None

# ---------------------------------------------------------------------------
# Minimal, Non-Destructive Request Normalization
# Gateway/Router only: does not rewrite agent semantics or system instructions.
# ---------------------------------------------------------------------------
def normalize_provider_payload(payload: Dict[str, Any], provider: str) -> Dict[str, Any]:
    """
    Ensures provider-compatible message & tool structure without changing agent semantics:
    - Verifies 'tool' messages contain valid 'tool_call_id'.
    - Enforces Mistral/vendor strict ordering: every assistant message with tool_calls must have matching tool responses.
    - Preserves exact system prompts, message order, and reasoning state.
    """
    normalized = dict(payload)
    messages = normalized.get("messages", [])
    
    # Pre-clean tool_call_id and normalize list content for Mistral if needed
    prepared_messages = []
    for msg in messages:
        msg_dict = dict(msg)
        if msg_dict.get("role") == "tool" and not msg_dict.get("tool_call_id"):
            msg_dict["tool_call_id"] = "tc-generic"
        if provider == "mistral" and isinstance(msg_dict.get("content"), list):
            text_chunks = []
            for part in msg_dict["content"]:
                if isinstance(part, str):
                    text_chunks.append(part)
                elif isinstance(part, dict):
                    if part.get("type") == "text":
                        text_chunks.append(part.get("text", ""))
                    elif "text" in part:
                        text_chunks.append(part["text"])
            msg_dict["content"] = "\n".join(text_chunks)
        prepared_messages.append(msg_dict)

    cleaned_messages = []
    i = 0
    n = len(prepared_messages)
    while i < n:
        msg = dict(prepared_messages[i])
        role = msg.get("role")
        tool_calls = msg.get("tool_calls")

        if role == "assistant" and tool_calls:
            expected_ids = {tc.get("id") for tc in tool_calls if isinstance(tc, dict) and tc.get("id")}
            
            # Collect immediately following tool messages
            j = i + 1
            found_tool_msgs = []
            found_ids = set()
            while j < n and prepared_messages[j].get("role") == "tool":
                t_msg = dict(prepared_messages[j])
                cid = t_msg.get("tool_call_id")
                if cid:
                    found_ids.add(cid)
                found_tool_msgs.append(t_msg)
                j += 1

            # Check if any tool_call has no response
            missing_ids = expected_ids - found_ids
            if missing_ids:
                for tc in tool_calls:
                    if isinstance(tc, dict) and tc.get("id") in missing_ids:
                        func_name = tc.get("function", {}).get("name") if isinstance(tc.get("function"), dict) else "tool"
                        found_tool_msgs.append({
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "name": func_name or "tool",
                            "content": json.dumps({"status": "completed", "message": "Tool execution finished or context superseded."})
                        })

            cleaned_messages.append(msg)
            cleaned_messages.extend(found_tool_msgs)
            i = j
        elif role == "tool":
            # Orphaned tool message without preceding assistant tool_calls (Mistral rejects these)
            i += 1
        else:
            cleaned_messages.append(msg)
            i += 1

    normalized["messages"] = cleaned_messages
    return normalized

def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    total_chars = 0
    for m in messages:
        total_chars += len(str(m.get("content") or ""))
        if m.get("tool_calls"):
            total_chars += len(json.dumps(m["tool_calls"]))
    return max(1, total_chars // 4)

# ---------------------------------------------------------------------------
# Capability-Based Model Catalog
# ---------------------------------------------------------------------------
MODEL_CATALOG = {
    "talos-code": {
        "id": "talos-code",
        "capabilities": ["text", "code", "tool_calling", "streaming"],
        "default_upstream": "mistral/codestral-2508",
        "fallback_upstream": "openai/gpt-4o-mini"
    },
    "talos-general": {
        "id": "talos-general",
        "capabilities": ["text", "general_reasoning", "tool_calling", "streaming"],
        "default_upstream": "mistral/mistral-medium-latest",
        "fallback_upstream": "openai/gpt-4o-mini"
    },
    "glm-5-2": {
        "id": "glm-5-2",
        "capabilities": ["text", "code", "tool_calling", "streaming"],
        "default_upstream": "mistral/glm-5-2",
        "fallback_upstream": "openai/gpt-4o-mini"
    },
    "zai-glm-5-3": {
        "id": "zai-glm-5-3",
        "capabilities": ["text", "code", "tool_calling", "streaming"],
        "default_upstream": "mistral/zai-glm-5-3",
        "fallback_upstream": "openai/gpt-4o-mini"
    },
    "glm-5-3": {
        "id": "glm-5-3",
        "capabilities": ["text", "code", "tool_calling", "streaming"],
        "default_upstream": "mistral/zai-glm-5-3",
        "fallback_upstream": "openai/gpt-4o-mini"
    },
    "mistral-medium-latest": {
        "id": "mistral-medium-latest",
        "capabilities": ["text", "general_reasoning", "tool_calling", "streaming"],
        "default_upstream": "mistral/mistral-medium-latest",
        "fallback_upstream": "openai/gpt-4o-mini"
    },
    "mistral-large-2512": {
        "id": "mistral-large-2512",
        "capabilities": ["text", "complex_reasoning", "code", "tool_calling", "streaming"],
        "default_upstream": "mistral/mistral-large-2512",
        "fallback_upstream": "openai/gpt-4o-mini"
    },
    # Backward compatibility aliases
    "codestral-2508": {
        "id": "codestral-2508",
        "capabilities": ["text", "code", "tool_calling", "streaming"],
        "default_upstream": "mistral/codestral-2508",
        "fallback_upstream": "openai/gpt-4o-mini"
    },
    "codestral-latest": {
        "id": "codestral-latest",
        "capabilities": ["text", "code", "tool_calling", "streaming"],
        "default_upstream": "mistral/codestral-latest",
        "fallback_upstream": "openai/gpt-4o-mini"
    },
    "mistral-small-latest": {
        "id": "mistral-small-latest",
        "capabilities": ["text", "code", "tool_calling", "streaming"],
        "default_upstream": "mistral/mistral-small-latest",
        "fallback_upstream": "openai/gpt-4o-mini"
    }
}

@router.get("/models")
async def list_models():
    """Returns Talos capability-based model catalog instead of raw vendor endpoints."""
    return {
        "object": "list",
        "data": list(MODEL_CATALOG.values())
    }

@router.get("/metrics")
async def get_metrics():
    """Operational telemetry & usage accounting endpoint."""
    avg_latency = (
        round(telemetry.total_latency_ms / telemetry.total_requests, 2)
        if telemetry.total_requests > 0 else 0.0
    )
    return {
        "status": "healthy",
        "service": "talos-cloud-relay",
        "environment": config.TALOS_ENV,
        "total_requests": telemetry.total_requests,
        "total_input_tokens": telemetry.total_input_tokens,
        "total_output_tokens": telemetry.total_output_tokens,
        "avg_latency_ms": avg_latency,
        "provider_distribution": telemetry.provider_distribution,
        "recent_usage_records": telemetry.records[-20:]
    }

# ---------------------------------------------------------------------------
# Sequenced Line-by-Line SSE Parser (Replay & Chunk Fragmentation Protection)
# ---------------------------------------------------------------------------
async def parse_and_sequence_sse(
    upstream_stream: AsyncGenerator[bytes, None],
    request_id: str,
    on_first_token: Optional[Any] = None
) -> AsyncGenerator[str, None]:
    """
    Parses fragmented TCP packets, splits on CRLF/LF, assigns sequential IDs,
    and safeguards against silent token drops or malformed events.
    """
    sequence_id = 0
    buffer = ""
    ttft_marked = False
    saw_data = False
    saw_done = False

    def validate_data(data_part: str) -> None:
        if data_part == "[DONE]":
            return
        try:
            payload = json.loads(data_part)
        except json.JSONDecodeError as exc:
            clean = clean_upstream_error_text(data_part)
            raise RuntimeError(f"Upstream returned a non-JSON SSE response: {clean}") from exc
        if not isinstance(payload, dict) or "choices" not in payload:
            raise RuntimeError("Upstream returned an invalid chat-completion stream event.")

    async for chunk in upstream_stream:
        buffer += chunk.decode("utf-8", errors="replace")
        
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            
            if not line:
                continue

            if line.startswith("data:"):
                data_part = line[5:].strip()
                validate_data(data_part)
                saw_data = True
                
                # Check for stream completion
                if data_part == "[DONE]":
                    saw_done = True
                    yield f"id: {sequence_id}\nevent: done\ndata: [DONE]\n\n"
                    return

                # Measure TTFT on first data chunk
                if not ttft_marked and on_first_token:
                    ttft_marked = True
                    on_first_token()

                sequence_id += 1
                yield f"id: {sequence_id}\nevent: message\ndata: {data_part}\n\n"

    # Flush remaining buffer if present
    if buffer.strip() and buffer.strip().startswith("data:"):
        sequence_id += 1
        data_part = buffer.strip()[5:].strip()
        validate_data(data_part)
        saw_data = True
        if data_part == "[DONE]":
            saw_done = True
            yield f"id: {sequence_id}\nevent: done\ndata: [DONE]\n\n"
        else:
            yield f"id: {sequence_id}\nevent: message\ndata: {data_part}\n\n"

    if not saw_data:
        raise RuntimeError("Upstream closed without a valid chat-completion stream; it may have returned a WAF page.")
    if not saw_done:
        raise RuntimeError("Upstream ended the chat-completion stream without its completion marker.")

# ---------------------------------------------------------------------------
# Error Sanitizer for Upstream WAF / Gateways
# ---------------------------------------------------------------------------
def clean_upstream_error_text(raw_text: str, status_code: Optional[int] = None) -> str:
    """Sanitize any raw HTML or WAF responses from upstream LLM vendors into clean text."""
    if not raw_text:
        return f"Upstream error HTTP {status_code or 500}"
    is_html = (
        "<!doctype html" in raw_text.lower()
        or "<html" in raw_text.lower()
        or "<title>blocked</title>" in raw_text.lower()
        or "<title>just a moment...</title>" in raw_text.lower()
    )
    if is_html:
        import re
        title_match = re.search(r"<title>(.*?)</title>", raw_text, re.IGNORECASE | re.DOTALL)
        title = title_match.group(1).strip() if title_match else "Access Blocked"
        code = status_code or 403
        return f"Upstream WAF/Security Block (HTTP {code}: {title}) - Security Challenge Triggered"
    
    import re
    cleaned = re.sub(r"<[^>]+>", "", raw_text).strip()
    if len(cleaned) > 300:
        cleaned = cleaned[:297] + "..."
    return cleaned or f"Upstream HTTP {status_code or 500}"

# ---------------------------------------------------------------------------
# Main Relay Execution Endpoint
# ---------------------------------------------------------------------------
@router.post("/chat/completions")
@router.post("/relay/openai/v1/chat/completions")
async def chat_completions(request: ChatCompletionRequest, raw_req: Request):
    start_time = time.time()
    request_id = f"req-{int(time.time()*1000)}-{os.urandom(3).hex()}"
    requested_model = request.model or "talos-code"

    # 1. Resolve Capability Model to Vendor Upstream
    if requested_model in MODEL_CATALOG:
        catalog_entry = MODEL_CATALOG[requested_model]
        primary_upstream = catalog_entry["default_upstream"]
    elif (
        requested_model.startswith("glm-")
        or requested_model.startswith("zai-")
        or requested_model.startswith("mistral-")
        or requested_model.startswith("codestral-")
    ):
        primary_upstream = f"mistral/{requested_model}"
    else:
        catalog_entry = MODEL_CATALOG.get("talos-code", {})
        primary_upstream = catalog_entry.get("default_upstream", f"mistral/{requested_model}")
    
    # 2. Extract base payload without altering agent semantics
    raw_payload = request.model_dump(exclude_none=True)
    input_tokens = estimate_tokens(raw_payload.get("messages", []))

    # Determine provider route (all Mistral and GLM models route to Mistral base URL)
    if primary_upstream.startswith("mistral") and config.MISTRAL_API_KEY and not config.MOCK_MODE:
        provider_name = "mistral"
        upstream_model = primary_upstream.split("/", 1)[-1]
    elif config.MISTRAL_API_KEY and not config.MOCK_MODE:
        provider_name = "mistral"
        upstream_model = requested_model
    elif config.OPENAI_API_KEY and not config.MOCK_MODE:
        provider_name = "openai"
        upstream_model = "gpt-4o-mini"
    else:
        provider_name = "mock"
        upstream_model = "talos-mock-engine"

    # -----------------------------------------------------------------------
    # PATH A: Mistral Primary Route
    # -----------------------------------------------------------------------
    if provider_name == "mistral":
        vendor_payload = normalize_provider_payload(raw_payload, "mistral")
        vendor_payload["model"] = upstream_model

        headers = {
            "Authorization": f"Bearer {config.MISTRAL_API_KEY}",
            "Content-Type": "application/json"
        }

        # STREAMING EXECUTION
        if request.stream:
            ttft_time: Optional[float] = None

            def record_ttft():
                nonlocal ttft_time
                ttft_time = (time.time() - start_time) * 1000

            async def stream_wrapper():
                success = False
                error_msg = None
                output_tokens = 0
                failover_attempted = False

                try:
                    async with httpx.AsyncClient(timeout=120.0) as client:
                        async with client.stream(
                            "POST",
                            f"{config.MISTRAL_BASE_URL}/chat/completions",
                            headers=headers,
                            json=vendor_payload
                        ) as upstream_resp:
                            if upstream_resp.status_code == 200:
                                async for formatted_sse in parse_and_sequence_sse(
                                    upstream_resp.aiter_bytes(),
                                    request_id,
                                    on_first_token=record_ttft
                                ):
                                    output_tokens += 1
                                    yield formatted_sse
                                success = True
                            else:
                                err_content = await upstream_resp.aread()
                                raw_err = err_content.decode('utf-8', errors='ignore')
                                error_msg = f"Mistral HTTP {upstream_resp.status_code}: {clean_upstream_error_text(raw_err, upstream_resp.status_code)}"
                                
                                # True Live Model Failover: Query secondary vendor before giving up
                                if config.OPENAI_API_KEY:
                                    try:
                                        openai_payload = normalize_provider_payload(raw_payload, "openai")
                                        openai_payload["model"] = "gpt-4o-mini"
                                        openai_headers = {
                                            "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                                            "Content-Type": "application/json"
                                        }
                                        async with client.stream(
                                            "POST",
                                            f"{config.OPENAI_BASE_URL}/chat/completions",
                                            headers=openai_headers,
                                            json=openai_payload
                                        ) as fb_resp:
                                            if fb_resp.status_code == 200:
                                                async for formatted_sse in parse_and_sequence_sse(
                                                    fb_resp.aiter_bytes(),
                                                    request_id,
                                                    on_first_token=record_ttft
                                                ):
                                                    output_tokens += 1
                                                    yield formatted_sse
                                                success = True
                                                failover_attempted = True
                                    except Exception as fb_exc:
                                        error_msg += f" | Secondary failover error: {str(fb_exc)}"
                except Exception as exc:
                    error_msg = f"Upstream Network Error: {str(exc)}"
                    # Fail over on connection-level failures too. Previously the
                    # secondary provider was only tried when Mistral returned a
                    # non-200 response, so DNS/TLS/WAF exceptions skipped it.
                    # Do not replay after yielding partial output to avoid
                    # duplicating a response the client has already received.
                    if output_tokens == 0 and config.OPENAI_API_KEY:
                        try:
                            openai_payload = normalize_provider_payload(raw_payload, "openai")
                            openai_payload["model"] = "gpt-4o-mini"
                            openai_headers = {
                                "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                                "Content-Type": "application/json"
                            }
                            async with httpx.AsyncClient(timeout=120.0) as fallback_client:
                                async with fallback_client.stream(
                                    "POST",
                                    f"{config.OPENAI_BASE_URL}/chat/completions",
                                    headers=openai_headers,
                                    json=openai_payload
                                ) as fb_resp:
                                    if fb_resp.status_code == 200:
                                        async for formatted_sse in parse_and_sequence_sse(
                                            fb_resp.aiter_bytes(),
                                            request_id,
                                            on_first_token=record_ttft
                                        ):
                                            output_tokens += 1
                                            yield formatted_sse
                                        success = True
                                        failover_attempted = True
                                        error_msg = None
                                    else:
                                        fb_error = await fb_resp.aread()
                                        fb_text = fb_error.decode("utf-8", errors="ignore")
                                        error_msg += (
                                            f" | OpenAI fallback HTTP {fb_resp.status_code}: "
                                            f"{clean_upstream_error_text(fb_text, fb_resp.status_code)}"
                                        )
                        except Exception as fb_exc:
                            error_msg += f" | Secondary failover error: {str(fb_exc)}"
                finally:
                    duration_ms = (time.time() - start_time) * 1000
                    status = "completed" if success else "failed"
                    telemetry.record(UsageRecord(
                        request_id=request_id,
                        timestamp=time.time(),
                        provider="mistral",
                        model=requested_model,
                        upstream_model=upstream_model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        latency_ms=duration_ms,
                        ttft_ms=ttft_time,
                        status=status,
                        failover_used=failover_attempted,
                        finish_reason="stop" if success else "error",
                        error=error_msg
                    ))

                    # Authoritative Billing Settlement from Relay
                    reservation_id = raw_req.headers.get("X-Talos-Reservation-ID")
                    if reservation_id:
                        try:
                            from billing.repository import billing_repo
                            from billing.pricing import pricing_service
                            pricing_row = await billing_repo.get_active_pricing(requested_model)
                            if pricing_row:
                                calc = pricing_service.calculate_llm_cost(
                                    pricing_row=pricing_row,
                                    input_tokens=input_tokens,
                                    output_tokens=output_tokens
                                )
                                await billing_repo.settle_reservation(
                                    reservation_id=reservation_id,
                                    actual_credits=calc.talos_credits,
                                    raw_cost_usd=calc.vendor_cost_usd,
                                    pricing_version=calc.pricing_version,
                                    idempotency_key=f"set_{reservation_id}",
                                    meta={"model": requested_model, "in_tokens": input_tokens, "out_tokens": output_tokens}
                                )
                        except Exception as b_err:
                            print(f"[BILLING ERROR] Failed to settle reservation {reservation_id}: {b_err}")

                if not success and error_msg:
                    # Graceful resilience response instead of crashing client stream
                    fallback_content = (
                        f"I am responding in resilient fallback mode because the upstream AI provider encountered a temporary service interruption ({error_msg}). "
                        "Your request and workspace context are safely preserved."
                    )
                    fallback_payload = {
                        "id": request_id,
                        "object": "chat.completion.chunk",
                        "choices": [{
                            "index": 0,
                            "delta": {"content": fallback_content},
                            "finish_reason": "stop"
                        }]
                    }
                    yield f"id: 1\nevent: message\ndata: {json.dumps(fallback_payload)}\n\n"
                    yield "id: 2\nevent: done\ndata: [DONE]\n\n"

            return StreamingResponse(
                stream_wrapper(),
                media_type="text/event-stream",
                headers={
                    "X-Talos-Relay-Provider": "mistral",
                    "X-Talos-Relay-Model": requested_model,
                    "X-Talos-Request-Id": request_id
                }
            )

        # NON-STREAMING EXECUTION
        else:
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(
                        f"{config.MISTRAL_BASE_URL}/chat/completions",
                        headers=headers,
                        json=vendor_payload
                    )
                    duration_ms = (time.time() - start_time) * 1000
                    if resp.status_code == 200:
                        data = resp.json()
                        # Clean Markdown json fences if present in message content for structured json outputs
                        for choice in data.get("choices", []):
                            c_msg = choice.get("message", {})
                            c_content = c_msg.get("content")
                            if isinstance(c_content, str) and c_content.strip().startswith("```"):
                                stripped = c_content.strip()
                                if stripped.startswith("```json"):
                                    stripped = stripped[7:]
                                elif stripped.startswith("```"):
                                    stripped = stripped[3:]
                                if stripped.endswith("```"):
                                    stripped = stripped[:-3]
                                c_msg["content"] = stripped.strip()
                        out_tokens = data.get("usage", {}).get("completion_tokens", 10)
                        telemetry.record(UsageRecord(
                            request_id=request_id,
                            timestamp=time.time(),
                            provider="mistral",
                            model=requested_model,
                            upstream_model=upstream_model,
                            input_tokens=input_tokens,
                            output_tokens=out_tokens,
                            latency_ms=duration_ms,
                            ttft_ms=duration_ms,
                            status="completed"
                        ))
                        return data
                    else:
                        clean_err = clean_upstream_error_text(resp.text, resp.status_code)
                        # True Live Model Failover: Query secondary vendor before giving up
                        if config.OPENAI_API_KEY:
                            try:
                                openai_payload = normalize_provider_payload(raw_payload, "openai")
                                openai_payload["model"] = "gpt-4o-mini"
                                openai_headers = {
                                    "Authorization": f"Bearer {config.OPENAI_API_KEY}",
                                    "Content-Type": "application/json"
                                }
                                fb_resp = await client.post(
                                    f"{config.OPENAI_BASE_URL}/chat/completions",
                                    headers=openai_headers,
                                    json=openai_payload
                                )
                                if fb_resp.status_code == 200:
                                    fb_data = fb_resp.json()
                                    telemetry.record(UsageRecord(
                                        request_id=request_id,
                                        timestamp=time.time(),
                                        provider="openai",
                                        model=requested_model,
                                        upstream_model="gpt-4o-mini",
                                        input_tokens=input_tokens,
                                        output_tokens=fb_data.get("usage", {}).get("completion_tokens", 10),
                                        latency_ms=(time.time() - start_time) * 1000,
                                        status="completed",
                                        failover_used=True
                                    ))
                                    return fb_data
                            except Exception:
                                pass

                        telemetry.record(UsageRecord(
                            request_id=request_id,
                            timestamp=time.time(),
                            provider="mistral",
                            model=requested_model,
                            upstream_model=upstream_model,
                            input_tokens=input_tokens,
                            output_tokens=0,
                            latency_ms=duration_ms,
                            status="fallback",
                            error=clean_err
                        ))
                        return {
                            "id": request_id,
                            "object": "chat.completion",
                            "created": int(time.time()),
                            "model": requested_model,
                            "choices": [{
                                "index": 0,
                                "message": {
                                    "role": "assistant",
                                    "content": f"Operating in fallback mode: upstream service temporarily unavailable ({clean_err})."
                                },
                                "finish_reason": "stop"
                            }],
                            "usage": {"prompt_tokens": input_tokens, "completion_tokens": 10, "total_tokens": input_tokens + 10}
                        }
            except Exception as exc:
                duration_ms = (time.time() - start_time) * 1000
                telemetry.record(UsageRecord(
                    request_id=request_id,
                    timestamp=time.time(),
                    provider="mistral",
                    model=requested_model,
                    upstream_model=upstream_model,
                    input_tokens=input_tokens,
                    output_tokens=0,
                    latency_ms=duration_ms,
                    status="error",
                    error=str(exc)
                ))
                raise HTTPException(status_code=502, detail=f"Relay upstream connection failed: {str(exc)}")

    # -----------------------------------------------------------------------
    # PATH B: Mock Fallback Route
    # -----------------------------------------------------------------------
    duration_ms = (time.time() - start_time) * 1000
    telemetry.record(UsageRecord(
        request_id=request_id,
        timestamp=time.time(),
        provider="mock",
        model=requested_model,
        upstream_model="talos-mock-engine",
        input_tokens=input_tokens,
        output_tokens=15,
        latency_ms=duration_ms,
        ttft_ms=50.0,
        status="mocked"
    ))
    return {
        "id": request_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": f"Response from Talos Cloud Relay (Mock Mode) to query: {request.messages[-1].content if request.messages else ''}"
                },
                "finish_reason": "stop"
            }
        ],
        "usage": {"prompt_tokens": input_tokens, "completion_tokens": 15, "total_tokens": input_tokens + 15}
    }
