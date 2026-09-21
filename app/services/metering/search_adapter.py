"""
Talos Cloud — Search, Browser & Image Metering Adapters.
"""

from typing import Any
from app.services.metering.base import MeteringEvent


# ─── Web Search Adapter ────────────────────────────────────────────────────────

def extract_search_usage(
    provider_response: dict[str, Any],
    capability_id: str = "web_search",
    provider: str = "tavily",
    model_id: str = "tavily-search",
    task_id: str | None = None,
    search_count: int = 1,
) -> list[MeteringEvent]:
    """
    Extract usage from a web search provider response.
    Each search call = 1 unit (per_call billing).
    """
    search_type = provider_response.get("search_type", "basic")
    return [MeteringEvent(
        capability_id=capability_id,
        provider=provider,
        model_id=model_id,
        unit_type="per_call",
        quantity=max(1, search_count),
        task_id=task_id,
        metadata={"search_type": search_type},
    )]


# ─── Browser Use Adapter ───────────────────────────────────────────────────────

def extract_browser_usage(
    browser_seconds: int,
    capability_id: str = "browser_use",
    provider: str = "internal",
    model_id: str = "talos-browser-runner",
    task_id: str | None = None,
) -> list[MeteringEvent]:
    """
    Extract usage from browser execution.
    Billed per minute (rounds up to nearest whole minute).
    """
    minutes = max(1, -(-browser_seconds // 60))  # ceiling division
    return [MeteringEvent(
        capability_id=capability_id,
        provider=provider,
        model_id=model_id,
        unit_type="per_minute",
        quantity=minutes,
        task_id=task_id,
        metadata={"browser_seconds": browser_seconds},
    )]


# ─── Image Generation Adapter ─────────────────────────────────────────────────

def extract_image_usage(
    provider_response: dict[str, Any],
    capability_id: str = "image_gen",
    provider: str = "zhipu",
    model_id: str = "cogview-4-250304",
    task_id: str | None = None,
    quality: str = "medium",
    image_count: int = 1,
) -> list[MeteringEvent]:
    """
    Extract usage from an image generation response.
    Billed per image at the requested quality tier (image_low/medium/high).
    """
    quality = quality.lower()
    if quality in ("low", "draft", "standard"):
        unit_type = "image_low"
    elif quality in ("high", "hd", "ultra"):
        unit_type = "image_high"
    else:
        unit_type = "image_medium"

    # Detect image count from response if available
    if "data" in provider_response and isinstance(provider_response["data"], list):
        image_count = max(1, len(provider_response["data"]))

    return [MeteringEvent(
        capability_id=capability_id,
        provider=provider,
        model_id=model_id,
        unit_type=unit_type,
        quantity=image_count,
        task_id=task_id,
        metadata={"quality": quality, "image_count": image_count},
    )]
