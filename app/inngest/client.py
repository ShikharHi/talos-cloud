"""
Talos Cloud — Inngest Central Client.

Initializes the official Inngest client for Talos using stable app ID: talos-cloud.
Supports local dev server detection via INNGEST_DEV=1 or config.
"""

from __future__ import annotations

import logging
import os
import inngest
from app.config import get_settings

logger = logging.getLogger("talos.inngest")

settings = get_settings()

is_dev = bool(
    os.environ.get("INNGEST_DEV") == "1"
    or os.environ.get("INNGEST_DEV", "").lower() in ("true", "yes")
    or settings.inngest_dev
    or settings.talos_env in ("development", "test")
)
is_production = not is_dev

# Initialize central Inngest client
inngest_client = inngest.Inngest(
    app_id=settings.inngest_app_id or "talos-cloud",
    is_production=is_production,
    event_key=settings.inngest_event_key or os.environ.get("INNGEST_EVENT_KEY"),
    signing_key=settings.inngest_signing_key or os.environ.get("INNGEST_SIGNING_KEY"),
    logger=logger,
)
