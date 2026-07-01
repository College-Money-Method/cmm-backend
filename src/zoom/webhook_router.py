"""Zoom webhook handler — URL validation and webinar.ended attendance sync."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from src.config import settings
from src.db.base import get_session_factory
from src.workshops.attendance_sync_service import sync_webinar_attendance

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/zoom", tags=["zoom-webhooks"])

# Delays (seconds) between retry attempts when Zoom report isn't ready yet
_RETRY_DELAYS = [0, 900, 1800]  # 0 min, 15 min, 30 min


def _verify_signature(raw_body: bytes, timestamp: str, signature: str) -> bool:
    """Verify Zoom webhook HMAC-SHA256 signature."""
    if not settings.zoom_webhook_secret_token:
        logger.warning("zoom_webhook_secret_token not configured — skipping signature check")
        return True

    message = f"v0:{timestamp}:{raw_body.decode('utf-8')}"
    expected = "v0=" + hmac.new(
        settings.zoom_webhook_secret_token.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _sync_with_retry(zoom_webinar_id: str) -> None:
    """Attempt attendance sync with retries to handle Zoom report delay."""
    SessionLocal = get_session_factory()
    for i, delay in enumerate(_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)

        db = SessionLocal()
        try:
            synced = sync_webinar_attendance(zoom_webinar_id, db)
            if synced:
                logger.info("Attendance sync succeeded on attempt %d — webinar=%s", i + 1, zoom_webinar_id)
                return
            logger.info(
                "Zoom report not ready (attempt %d/%d) — webinar=%s",
                i + 1,
                len(_RETRY_DELAYS),
                zoom_webinar_id,
            )
        except Exception as exc:
            logger.error("Attendance sync error (attempt %d) — webinar=%s error=%s", i + 1, zoom_webinar_id, exc)
        finally:
            db.close()

    logger.warning(
        "Attendance sync exhausted retries — webinar=%s will need manual sync",
        zoom_webinar_id,
    )


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def zoom_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receives Zoom webhook events.

    Handles:
    - ``endpoint.url_validation``: Zoom challenge-response to activate the subscription.
    - ``webinar.ended``: kicks off an async attendance sync (with retries for report delay).
    """
    raw_body = await request.body()
    timestamp = request.headers.get("x-zm-request-timestamp", "")
    signature = request.headers.get("x-zm-signature", "")

    if not _verify_signature(raw_body, timestamp, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    payload = json.loads(raw_body)
    event = payload.get("event")

    # Zoom URL validation challenge — must respond before subscription activates
    if event == "endpoint.url_validation":
        plain_token = payload.get("payload", {}).get("plainToken", "")
        encrypted = hmac.new(
            settings.zoom_webhook_secret_token.encode(),
            plain_token.encode(),
            hashlib.sha256,
        ).hexdigest()
        return {"plainToken": plain_token, "encryptedToken": encrypted}

    if event == "webinar.ended":
        zoom_webinar_id = str(payload.get("payload", {}).get("object", {}).get("id", ""))
        if zoom_webinar_id:
            logger.info("webinar.ended received — scheduling attendance sync for webinar=%s", zoom_webinar_id)
            background_tasks.add_task(_sync_with_retry, zoom_webinar_id)
        else:
            logger.warning("webinar.ended payload missing object.id — payload=%s", payload)

    # Always return 200 so Zoom doesn't retry unhandled event types
    return {"status": "ok"}
