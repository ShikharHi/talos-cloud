"""
Talos Cloud — Auth & Notification Inngest Functions.

Provides:
  1. talos.auth.send_email [Events: verification, password reset, security alert]
     - Dispatches transactional emails via SMTP
     - Configurable retries with exponential backoff

  2. talos.auth.cleanup_expired_tokens [Scheduled: 0 * * * *]
     - Purges expired device tokens and web sessions from PostgreSQL
     - Uses the cloud's own DB (no talos-backend dependency)
"""

from __future__ import annotations

import logging
from typing import Any

import inngest

from app.inngest.client import inngest_client
from app.inngest.events import TalosEvents

logger = logging.getLogger("talos.inngest.auth")


@inngest_client.create_function(
    fn_id="talos.auth.send_email",
    name="Talos Auth: Send Transactional Email",
    trigger=inngest.TriggerEvent(
        event=TalosEvents.AUTH_EMAIL_VERIFICATION
    ),
    retries=3,
)
async def auth_send_verification_email_fn(
    ctx: inngest.Context,
    step: inngest.Step,
) -> dict[str, Any]:
    data = ctx.event.data
    email = data.get("email")
    token = data.get("token")
    if not email or not token:
        raise inngest.NonRetriableError("Email and token are required")

    async def _send():
        # Import EmailService safely — only available in local dev (talos-backend)
        import sys
        from pathlib import Path
        backend_dir = Path(__file__).resolve().parent.parent.parent.parent / "talos-backend"
        if not backend_dir.exists():
            logger.warning(
                "talos-backend not available in this environment — "
                "verification email for %s not sent", email
            )
            return False
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        from auth.email_service import EmailService
        return await EmailService.send_verification_email(email, token)

    success = await step.run("send-verification-email", _send)
    return {"status": "sent" if success else "logged", "email": email}


@inngest_client.create_function(
    fn_id="talos.auth.send_password_reset",
    name="Talos Auth: Send Password Reset Email",
    trigger=inngest.TriggerEvent(
        event=TalosEvents.AUTH_EMAIL_PASSWORD_RESET
    ),
    retries=3,
)
async def auth_send_password_reset_email_fn(
    ctx: inngest.Context,
    step: inngest.Step,
) -> dict[str, Any]:
    data = ctx.event.data
    email = data.get("email")
    token = data.get("token")
    if not email or not token:
        raise inngest.NonRetriableError("Email and token are required")

    async def _send():
        import sys
        from pathlib import Path
        backend_dir = Path(__file__).resolve().parent.parent.parent.parent / "talos-backend"
        if not backend_dir.exists():
            logger.warning(
                "talos-backend not available in this environment — "
                "password reset email for %s not sent", email
            )
            return False
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        from auth.email_service import EmailService
        return await EmailService.send_password_reset_email(email, token)

    success = await step.run("send-password-reset-email", _send)
    return {"status": "sent" if success else "logged", "email": email}


@inngest_client.create_function(
    fn_id="talos.auth.send_security_alert",
    name="Talos Auth: Send Security Alert",
    trigger=inngest.TriggerEvent(
        event=TalosEvents.AUTH_SECURITY_ALERT
    ),
    retries=3,
)
async def auth_send_security_alert_fn(
    ctx: inngest.Context,
    step: inngest.Step,
) -> dict[str, Any]:
    data = ctx.event.data
    email = data.get("email")
    subject = data.get("subject", "Talos Security Alert")
    body = data.get("body", "")
    if not email:
        raise inngest.NonRetriableError("Email is required")

    async def _send():
        import sys
        from pathlib import Path
        backend_dir = Path(__file__).resolve().parent.parent.parent.parent / "talos-backend"
        if not backend_dir.exists():
            logger.warning(
                "talos-backend not available in this environment — "
                "security alert for %s not sent", email
            )
            return False
        if str(backend_dir) not in sys.path:
            sys.path.insert(0, str(backend_dir))
        from auth.email_service import EmailService
        return await EmailService.send_security_notification(email, subject, body)

    success = await step.run("send-security-alert", _send)
    return {"status": "sent" if success else "logged", "email": email}


@inngest_client.create_function(
    fn_id="talos.auth.cleanup_expired_tokens",
    name="Talos Auth: Cleanup Expired Tokens & Sessions",
    trigger=inngest.TriggerCron(cron="0 * * * *"),
    retries=3,
    concurrency=[
        inngest.Concurrency(
            scope="fn",
            limit=1,
        )
    ],
)
async def auth_cleanup_expired_tokens_fn(
    ctx: inngest.Context,
    step: inngest.Step,
) -> dict[str, Any]:
    """
    Scheduled hourly to clean up expired device tokens and web sessions.
    Uses the cloud's own PostgreSQL via SQLAlchemy — no talos-backend dependency.
    """
    async def _purge() -> dict[str, int]:
        from datetime import datetime, timezone
        from sqlalchemy import delete, or_
        from app.database import get_session_factory
        from app.models.accounts import DeviceToken, WebSessionRecord

        now = datetime.now(timezone.utc)
        stats: dict[str, int] = {}

        factory = get_session_factory()
        async with factory() as session:
            # Delete expired or revoked device tokens
            result = await session.execute(
                delete(DeviceToken).where(
                    or_(
                        DeviceToken.expires_at <= now,
                        DeviceToken.revoked.is_(True),
                    )
                )
            )
            stats["expired_device_tokens"] = result.rowcount

            # Delete expired web sessions (revoked or past expiry)
            result = await session.execute(
                delete(WebSessionRecord).where(
                    or_(
                        WebSessionRecord.expires_at <= now,
                        WebSessionRecord.revoked_at.is_not(None),
                    )
                )
            )
            stats["expired_web_sessions"] = result.rowcount

            await session.commit()

        return stats

    result = await step.run("cleanup-tokens", _purge)
    logger.info("Auth Inngest: Cleanup completed: %s", result)
    return result
