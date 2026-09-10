"""Turn "Zoom has a finished recording" into a dispatched job.

One entry point for both ways a recording reaches us: the `recording.completed`
webhook, and the hourly reconcile sweep that catches the deliveries the webhook
missed. Sharing the path means the idempotency guarantee, the unknown-webinar
handling and the dispatch decision are written once.

Runs as a background task, never in the request cycle: the webhook handler must
return inside 2 s, and Zoom disables an endpoint that is slow or errors.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from src.db.base import get_session_factory
from src.video_pipeline import job_service, task_dispatch
from src.workshops.models import Webinar

logger = logging.getLogger(__name__)


def intake_recording(zoom_webinar_id: str, zoom_recording_uuid: str) -> bool:
    """Create (idempotently) and dispatch a job for one Zoom recording.

    Returns True when a new job row was created — False for a duplicate, an
    unrecognised webinar, or any error. Never raises: both callers are
    fire-and-forget, and the alternative to swallowing here is an unhandled
    exception in a background task that nothing reports.
    """
    if not zoom_webinar_id or not zoom_recording_uuid:
        logger.warning(
            "Recording intake missing identifiers — webinar=%r recording=%r",
            zoom_webinar_id,
            zoom_recording_uuid,
        )
        return False

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        webinar = db.execute(
            select(Webinar).where(Webinar.zoom_webinar_id == str(zoom_webinar_id))
        ).scalar_one_or_none()

        if webinar is None:
            # Zoom hosts meetings this app does not know about (internal calls,
            # test webinars). Not an error — just nothing to publish a replay to.
            logger.warning(
                "Recording for unrecognised webinar — zoom_webinar_id=%s recording=%s",
                zoom_webinar_id,
                zoom_recording_uuid,
            )
            return False

        job, created = job_service.create_from_recording(
            db,
            webinar_id=webinar.id,
            zoom_recording_uuid=zoom_recording_uuid,
        )
        if not created:
            logger.info(
                "Recording already has a job — job=%s state=%s recording=%s",
                job.id,
                job.state,
                zoom_recording_uuid,
            )
            return False

        task_dispatch.dispatch(db, job)
        return True

    except Exception as exc:
        logger.exception(
            "Recording intake failed — zoom_webinar_id=%s recording=%s error=%s",
            zoom_webinar_id,
            zoom_recording_uuid,
            exc,
        )
        return False
    finally:
        db.close()
