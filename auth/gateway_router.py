import httpx
import uuid
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import Optional

from auth.repository import auth_repo
from auth.service import get_current_user, validate_csrf

router = APIRouter(prefix="/v1", tags=["gateway"])

BACKEND_BASE_URL = "http://localhost:8001"
INTERNAL_SECRET = "talos_internal_default_secret_key_v6"

class ChatRequest(BaseModel):
    prompt: str
    thread_id: Optional[str] = None
    model: Optional[str] = "talos-code"

@router.post("/chat")
async def public_chat_endpoint(
    req: ChatRequest,
    request: Request,
    current_user: dict = Depends(get_current_user)
):
    """
    Public Chat Gateway:
    1. Authenticates user session via HttpOnly cookie
    2. Assigns distributed X-Talos-Request-ID
    3. Indexes thread ownership in Cloud DB
    4. Forwards to Private Backend with X-Talos-User-ID, Request-ID, Internal-Token
    5. Streams SSE back to client
    """
    validate_csrf(request)

    if not req.prompt:
        raise HTTPException(status_code=400, detail="Prompt is required")

    user_id = current_user["id"]
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    thread_id = req.thread_id or f"thread_{uuid.uuid4().hex[:12]}"

    # Index thread in Cloud DB
    title_summary = req.prompt.strip()[:32]
    await auth_repo.index_thread(thread_id, user_id, title_summary)

    # Dynamic Pre-Flight Funding & COGS Gate
    from billing.repository import billing_repo
    wallet = await billing_repo.get_wallet(user_id)
    subscription = await billing_repo.get_subscription(user_id)
    from decimal import Decimal
    settled = Decimal(str(wallet.get("settled_balance", "0")))
    reserved = Decimal(str(wallet.get("active_reserved", "0")))
    spendable = settled - reserved

    # If subscription exists, check if actual COGS reached ceiling
    if subscription:
        act_cogs = Decimal(str(subscription.get("period_actual_cogs_usd", "0")))
        ceil_cogs = Decimal(str(subscription.get("period_cogs_ceiling_usd", "5.0")))
        if act_cogs >= ceil_cogs:
            raise HTTPException(
                status_code=402,
                detail="Monthly provider-cost ceiling ($5.00) reached. Please renew or wait for next billing cycle."
            )

    # Check if spendable credits can fund at least an initial turn (e.g. 0.05 credits)
    if spendable <= Decimal("0"):
        raise HTTPException(
            status_code=402,
            detail="Your credit balance is exhausted. Please add credits or renew your subscription to continue."
        )

    client = httpx.AsyncClient(timeout=120.0)

    async def sse_generator():
        try:
            headers = {
                "Content-Type": "application/json",
                "X-Talos-Internal-Token": INTERNAL_SECRET,
                "X-Talos-User-ID": user_id,
                "X-Talos-Request-ID": request_id,
            }
            payload = {
                "prompt": req.prompt,
                "thread_id": thread_id,
                "model": req.model or "talos-code"
            }
            async with client.stream(
                "POST",
                f"{BACKEND_BASE_URL}/ag-ui",
                json=payload,
                headers=headers
            ) as resp:
                if resp.status_code != 200:
                    err_msg = await resp.aread()
                    yield f"event: RUN_ERROR\ndata: {{\"error\": \"Backend error {resp.status_code}: {err_msg.decode()}\"}}\n\n"
                    return

                async for line in resp.aiter_lines():
                    if line:
                        yield f"{line}\n"
                    else:
                        yield "\n"
        except Exception as e:
            yield f"event: RUN_ERROR\ndata: {{\"error\": \"Gateway stream failure: {str(e)}\"}}\n\n"
        finally:
            await client.aclose()

    return StreamingResponse(sse_generator(), media_type="text/event-stream")

@router.get("/threads")
async def list_user_threads(current_user: dict = Depends(get_current_user)):
    """List threads belonging exclusively to the authenticated user."""
    threads = await auth_repo.get_user_threads(current_user["id"])
    return {"status": "success", "threads": threads}

@router.delete("/threads/{thread_id}")
async def delete_thread(
    thread_id: str,
    request: Request,
    current_user: dict = Depends(get_current_user)
):
    """
    Delete a specific thread:
    1. Validates thread ownership (403 if thread belongs to someone else)
    2. Deletes row from Cloud thread_index
    3. Calls Backend to purge short-term checkpoints for this thread (Preserves user's long-term memory)
    """
    validate_csrf(request)
    user_id = current_user["id"]
    owner = await auth_repo.get_thread_owner(thread_id)

    if owner and owner != user_id:
        raise HTTPException(status_code=403, detail="Forbidden: You do not own this chat thread")

    # Remove from Cloud thread index
    await auth_repo.delete_thread_index(thread_id, user_id)

    # Call private backend to purge short-term checkpoints
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    headers = {
        "X-Talos-Internal-Token": INTERNAL_SECRET,
        "X-Talos-User-ID": user_id,
        "X-Talos-Request-ID": request_id,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.delete(f"{BACKEND_BASE_URL}/internal/threads/{thread_id}", headers=headers)
        except Exception as e:
            pass

    return {"status": "success", "message": f"Thread {thread_id} deleted"}

@router.delete("/memories")
async def clear_user_memories(
    request: Request,
    current_user: dict = Depends(get_current_user)
):
    """
    Clear all learned long-term memories for this user:
    1. Does NOT delete chat threads or checkpoints.
    2. Calls Backend to purge the user's memory namespace.
    """
    validate_csrf(request)
    user_id = current_user["id"]
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    headers = {
        "X-Talos-Internal-Token": INTERNAL_SECRET,
        "X-Talos-User-ID": user_id,
        "X-Talos-Request-ID": request_id,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.delete(f"{BACKEND_BASE_URL}/internal/memories", headers=headers)
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to clear memories: {str(e)}")

    return {"status": "success", "message": "Learned long-term memories purged"}

@router.get("/workspaces/{thread_id}/files")
async def get_workspace_files_proxy(
    thread_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Proxy workspace file list request to private backend."""
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    headers = {
        "X-Talos-Internal-Token": INTERNAL_SECRET,
        "X-Talos-User-ID": current_user["id"],
        "X-Talos-Request-ID": request_id,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{BACKEND_BASE_URL}/internal/workspaces/{thread_id}/files", headers=headers)
            return resp.json()
        except Exception as e:
            return {"thread_id": thread_id, "files": []}

@router.get("/workspaces/{thread_id}/file")
async def get_workspace_file_content_proxy(
    thread_id: str,
    path: str,
    current_user: dict = Depends(get_current_user)
):
    """Proxy workspace single file content request to private backend."""
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    headers = {
        "X-Talos-Internal-Token": INTERNAL_SECRET,
        "X-Talos-User-ID": current_user["id"],
        "X-Talos-Request-ID": request_id,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{BACKEND_BASE_URL}/internal/workspaces/{thread_id}/file",
                params={"path": path},
                headers=headers
            )
            return resp.json()
        except Exception as e:
            raise HTTPException(status_code=404, detail="File not found")

@router.get("/workspaces/{thread_id}/raw")
async def get_workspace_raw_file_proxy(
    thread_id: str,
    path: str,
    current_user: dict = Depends(get_current_user)
):
    """Proxy workspace raw file download/stream request."""
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    headers = {
        "X-Talos-Internal-Token": INTERNAL_SECRET,
        "X-Talos-User-ID": current_user["id"],
        "X-Talos-Request-ID": request_id,
    }
    client = httpx.AsyncClient(timeout=30.0)
    try:
        req = client.build_request(
            "GET",
            f"{BACKEND_BASE_URL}/internal/workspaces/{thread_id}/raw",
            params={"path": path},
            headers=headers
        )
        resp = await client.send(req, stream=True)
        filename = path.replace("\\", "/").split("/")[-1]
        return StreamingResponse(
            resp.aiter_raw(),
            status_code=resp.status_code,
            headers={
                "content-type": resp.headers.get("content-type", "application/octet-stream"),
                "content-disposition": resp.headers.get("content-disposition", f'inline; filename="{filename}"')
            },
            background=client.aclose
        )
    except Exception as e:
        await client.aclose()
        raise HTTPException(status_code=404, detail="File not found")


@router.get("/workspaces/{thread_id}/terminal")
async def get_workspace_terminal_logs_proxy(
    thread_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Proxy terminal logs request to private backend."""
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    headers = {
        "X-Talos-Internal-Token": INTERNAL_SECRET,
        "X-Talos-User-ID": current_user["id"],
        "X-Talos-Request-ID": request_id,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{BACKEND_BASE_URL}/internal/workspaces/{thread_id}/terminal", headers=headers)
            return resp.json()
        except Exception as e:
            return {"thread_id": thread_id, "is_running": False, "logs": "Terminal unavailable"}

@router.post("/workspaces/{thread_id}/terminal/stop")
async def stop_workspace_process_proxy(
    thread_id: str,
    current_user: dict = Depends(get_current_user)
):
    """Proxy stop process request to private backend."""
    request_id = f"req_{uuid.uuid4().hex[:12]}"
    headers = {
        "X-Talos-Internal-Token": INTERNAL_SECRET,
        "X-Talos-User-ID": current_user["id"],
        "X-Talos-Request-ID": request_id,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(f"{BACKEND_BASE_URL}/internal/workspaces/{thread_id}/terminal/stop", headers=headers)
            return resp.json()
        except Exception as e:
            return {"status": "error", "message": str(e)}
