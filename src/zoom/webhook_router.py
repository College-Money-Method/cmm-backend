"""Zoom webhook handler — URL validation, webinar.ended attendance and Q&A
syncs, recording video pipeline triggers."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status

from src.config import settings
from src.db.base import get_session_factory
from src.video_pipeline.intake import intake_recording
from src.workshops.attendance_sync_service import sync_webinar_attendance
from src.workshops.qa_sync_service import sync_webinar_qa

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/zoom", tags=["zoom-webhooks"])

# Delays (seconds) between retry attempts when Zoom report isn't ready yet
_RETRY_DELAYS = [0, 900, 1800]  # 0 min, 15 min, 30 min

# Everything `webinar.ended` pulls from the Zoom Reports API, by display name.
# Each takes `(zoom_webinar_id, db)` and returns True once it has the report.
_POST_WEBINAR_SYNCS = {
    "attendance": sync_webinar_attendance,
    "Q&A": sync_webinar_qa,
}

# Both events mean "there is a recording worth publishing", and both carry the
# same object identifiers, so both go through the same idempotent intake.
#
# `transcript_completed` is here as a second chance rather than a better signal.
# Zoom acknowledges `recording.completed` can misfire, and a delivery it drops
# would otherwise wait for the hourly reconcile sweep. It also arrives strictly
# after the video files are processed, so a job created by it never meets the
# "still being processed" refusal that a job created by the earlier event can.
#
# It does not fire at all when the account has audio transcripts switched off,
# which is why it can only ever be an addition to `recording.completed`.
_RECORDING_EVENTS = ("recording.completed", "recording.transcript_completed")


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
    """Run every post-webinar Zoom report sync, retrying past the report delay.

    The two reports come from the same Reports API and share the same 5-30 min
    availability lag, so they share one ladder rather than each sleeping through
    their own. They are otherwise independent: each attempt gets its own session
    and its own ``except``, a sync that succeeds drops out of the remaining
    attempts, and one that keeps failing cannot hold back or roll back the other.
    """
    SessionLocal = get_session_factory()
    pending = dict(_POST_WEBINAR_SYNCS)

    for i, delay in enumerate(_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)

        for name, sync in list(pending.items()):
            db = SessionLocal()
            try:
                if await asyncio.to_thread(sync, zoom_webinar_id, db):
                    del pending[name]
                    logger.info(
                        "%s sync succeeded on attempt %d — webinar=%s", name, i + 1, zoom_webinar_id
                    )
                else:
                    logger.info(
                        "Zoom %s report not ready (attempt %d/%d) — webinar=%s",
                        name,
                        i + 1,
                        len(_RETRY_DELAYS),
                        zoom_webinar_id,
                    )
            except Exception as exc:
                logger.error(
                    "%s sync error (attempt %d) — webinar=%s error=%s",
                    name,
                    i + 1,
                    zoom_webinar_id,
                    exc,
                )
            finally:
                db.close()

        if not pending:
            return

    logger.warning(
        "Post-webinar sync exhausted retries — webinar=%s unfinished=%s, will need manual sync",
        zoom_webinar_id,
        ", ".join(pending),
    )


def _schedule_recording_intake(
    payload: dict, background_tasks: BackgroundTasks, event: str
) -> None:
    """Queue video pipeline intake for a finished cloud recording.

    ``object.uuid`` is the per-instance recording UUID and the pipeline's
    idempotency key; ``object.id`` is the webinar number that maps to a
    ``Webinar`` row. The DB work happens in a background task because the
    handler must answer inside 2 s, and because ``recording.completed`` fires
    when Zoom finishes *processing* — routinely minutes and occasionally hours
    after the webinar ended, so nothing here may assume fresh timing.

    The webhook's ``download_token`` is deliberately ignored rather than
    persisted: the task re-fetches a fresh download URL over S2S OAuth, which
    leaves no Zoom credential at rest.

    Intake is idempotent on the recording UUID, which is what lets both
    recording events arrive here. The second one to land finds the job already
    made and does nothing — deliberately including no re-dispatch of a job still
    waiting in `pending`. The sweeper is the one process that dispatches those,
    and a webhook that raced it would put two ECS tasks on one recording, which
    is the failure this pipeline has already been bitten by once.
    """
    obj = payload.get("payload", {}).get("object", {})
    recording_uuid = str(obj.get("uuid") or "")
    zoom_webinar_id = str(obj.get("id") or "")

    if not recording_uuid or not zoom_webinar_id:
        logger.warning("%s payload missing object.uuid/object.id — payload=%s", event, payload)
        return

    logger.info(
        "%s received — scheduling video job intake webinar=%s recording=%s",
        event,
        zoom_webinar_id,
        recording_uuid,
    )
    background_tasks.add_task(intake_recording, zoom_webinar_id, recording_uuid)


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def zoom_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Receives Zoom webhook events.

    Handles:
    - ``endpoint.url_validation``: Zoom challenge-response to activate the subscription.
    - ``webinar.ended``: kicks off the async attendance and Q&A syncs (with
      retries for report delay).
    - ``recording.completed`` and ``recording.transcript_completed``: create and
      dispatch a webinar video pipeline job, idempotently on the recording UUID.
    """
    raw_body = await request.body()
    payload = json.loads(raw_body)
    event = payload.get("event")

    # URL validation must be handled first — it is a bootstrapping step that
    # Zoom sends before the subscription is active, so signature check is skipped.
    if event == "endpoint.url_validation":
        plain_token = payload.get("payload", {}).get("plainToken", "")
        encrypted = hmac.new(
            settings.zoom_webhook_secret_token.encode(),
            plain_token.encode(),
            hashlib.sha256,
        ).hexdigest()
        logger.info("Zoom URL validation challenge received — responding")
        return {"plainToken": plain_token, "encryptedToken": encrypted}

    # All other events require a valid signature
    timestamp = request.headers.get("x-zm-request-timestamp", "")
    signature = request.headers.get("x-zm-signature", "")
    if not _verify_signature(raw_body, timestamp, signature):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid webhook signature")

    if event == "webinar.ended":
        zoom_webinar_id = str(payload.get("payload", {}).get("object", {}).get("id", ""))
        if zoom_webinar_id:
            logger.info(
                "webinar.ended received — scheduling attendance and Q&A sync for webinar=%s",
                zoom_webinar_id,
            )
            background_tasks.add_task(_sync_with_retry, zoom_webinar_id)
        else:
            logger.warning("webinar.ended payload missing object.id — payload=%s", payload)

    elif event in _RECORDING_EVENTS:
        # Never let a handler error reach the response. Zoom disables an
        # endpoint that returns non-2xx repeatedly, and losing the webhook
        # entirely is far worse than losing one recording to the hourly
        # reconcile sweep, which would pick this up anyway.
        try:
            _schedule_recording_intake(payload, background_tasks, event)
        except Exception as exc:
            logger.exception("%s handling failed — error=%s", event, exc)

    # Always return 200 so Zoom doesn't retry unhandled event types
    return {"status": "ok"}
