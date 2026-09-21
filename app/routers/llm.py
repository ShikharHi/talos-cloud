"""
Talos Cloud — OpenAI-Compatible LLM Proxy Endpoint (`/v1/chat/completions` & `/v1/llm/chat/completions`).

Routes standard OpenAI chat requests (LangChain ChatOpenAI, OpenAI SDK, etc.)
directly through RelayService with full token metering, credit deduction, and streaming.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.accounts import Account
from app.routers.relay import get_authenticated_account
from app.services.relay_service import RelayService

router = APIRouter(prefix="/v1/llm", tags=["LLM Proxy"])
router_v1 = APIRouter(prefix="/v1", tags=["OpenAI Compatible v1"])


class ChatCompletionRequest(BaseModel):
    model: str = Field(default="glm-4.5-flash", description="Model name or capability")
    messages: list = Field(default_factory=list, description="List of chat messages")
    temperature: Optional[float] = 0.7
    max_tokens: Optional[int] = 4000
    stream: Optional[bool] = False
    run_id: Optional[str] = None
    agent_id: Optional[str] = None
    tools: Optional[list] = None
    tool_choice: Optional[Any] = None


@router.post("/chat/completions")
@router_v1.post("/chat/completions")
async def chat_completions(
    req: ChatCompletionRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Account = Depends(get_authenticated_account),
    x_idempotency_key: Optional[str] = Header(None, alias="X-Idempotency-Key"),
):
    model_lower = (req.model or "").lower()
    if any(k in model_lower for k in ("4o", "claude-3-5", "o1", "reasoning", "sonnet", "gemini-1.5-pro")):
        capability_id = "reasoning"
    else:
        capability_id = "fast_model"

    payload: dict[str, Any] = {
        "messages": req.messages,
        "model": req.model,
        "temperature": req.temperature or 0.7,
        "stream": req.stream or False,
    }
    if req.max_tokens:
        payload["max_tokens"] = req.max_tokens
    if req.tools:
        payload["tools"] = req.tools
    if req.tool_choice:
        payload["tool_choice"] = req.tool_choice

    service = RelayService(db)
    if req.stream:
        stream_iter = service.stream_call(
            account_id=current_user.account_id,
            task_id=req.run_id or req.agent_id,
            capability_id=capability_id,
            payload=payload,
            worst_case_units=req.max_tokens or 4000,
            idempotency_key=x_idempotency_key,
        )
        return StreamingResponse(stream_iter, media_type="text/event-stream")
    else:
        resp = await service.call(
            account_id=current_user.account_id,
            task_id=req.run_id or req.agent_id,
            capability_id=capability_id,
            payload=payload,
            worst_case_units=req.max_tokens or 4000,
            idempotency_key=x_idempotency_key,
        )
        return resp.get("result") or resp

