from typing import Optional
from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from marketplace.repositories.analytics import AnalyticsRepository
from marketplace.services.analytics import generate_visitor_hash

router = APIRouter(prefix="/v1/marketplace/events", tags=["Analytics Events"])
analytics_repo = AnalyticsRepository()

class EventPayload(BaseModel):
    kind: str = Field(..., description="plugin_view | builder_view | share_click | install_click")
    plugin_id: Optional[str] = None
    builder_id: Optional[str] = None
    referrer: Optional[str] = None
    utm_source: Optional[str] = None

@router.post("")
async def record_analytics_event(payload: EventPayload, request: Request):
    """High-throughput analytics event ingestion with salted daily rotating visitor hashing."""
    client_ip = request.client.host if request.client else "127.0.0.1"
    ua = request.headers.get("user-agent", "")
    visitor_hash = generate_visitor_hash(client_ip, ua)

    await analytics_repo.record_event(
        kind=payload.kind,
        visitor_hash=visitor_hash,
        plugin_id=payload.plugin_id,
        builder_id=payload.builder_id,
        referrer=payload.referrer,
        utm_source=payload.utm_source
    )
    return {"status": "recorded"}
