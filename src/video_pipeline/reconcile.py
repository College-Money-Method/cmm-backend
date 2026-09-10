"""Hourly sweep for Zoom recordings that never produced a job row.

Webhooks get missed — endpoint downtime during a deploy, a Zoom incident, a
delivery Zoom stopped retrying. A missed `recording.completed` leaves a
recording sitting in the cloud storage pool with nothing to process or delete
it, and that is the failure worth guarding against: a full pool blocks Zoom
from cloud-recording *future* webinars, which no retry can undo, unlike a
failed chaptering run.

The sweep is a left join, not a diff: list what Zoom holds, drop the ones that
already have a job, create the rest. Recordings whose webinar this app does not
know about are logged and skipped by ``intake_recording``, so an internal Zoom
call does not generate noise beyond one line.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.db.base import get_session_factory
from src.integrations.zoom import list_account_recordings
from src.video_pipeline.intake import intake_recording
from src.video_pipeline.models import WebinarVideoJob

logger = logging.getLogger(__name__)

# How far back to look. Wide enough to cover a multi-hour Zoom processing lag
# plus a deploy window, narrow enough that the listing stays one page.
_LOOKBACK_HOURS = 48


def reconcile_recordings(lookback_hours: int = _LOOKBACK_HOURS) -> int:
    """Create jobs for recent cloud recordings that have none. Returns the count.

    Never raises — it runs on a scheduler with nothing to catch it.
    """
    try:
        now = datetime.now(timezone.utc)
        from_date = (now - timedelta(hours=lookback_hours)).strftime("%Y-%m-%d")
        to_date = now.strftime("%Y-%m-%d")

        recordings = list_account_recordings(from_date, to_date)
        if recordings is None:
            # Distinct from an empty list: the listing failed, so "no orphans"
            # is not something we know. Do nothing rather than conclude.
            logger.warning("Recording reconcile skipped — Zoom listing unavailable")
            return 0

        if not recordings:
            logger.info("Recording reconcile — Zoom reported no recordings in window")
            return 0

        known = _known_recording_uuids(
            [str(r.get("uuid", "")) for r in recordings if r.get("uuid")]
        )

        created = 0
        for recording in recordings:
            recording_uuid = str(recording.get("uuid") or "")
            zoom_webinar_id = str(recording.get("id") or "")
            if not recording_uuid or recording_uuid in known:
                continue
            if intake_recording(zoom_webinar_id, recording_uuid):
                created += 1

        logger.info(
            "Recording reconcile complete — listed=%d orphans_created=%d",
            len(recordings),
            created,
        )
        return created

    except Exception as exc:
        logger.exception("Recording reconcile failed — error=%s", exc)
        return 0


def _known_recording_uuids(candidates: list[str]) -> set[str]:
    """Which of ``candidates`` already have a job row."""
    if not candidates:
        return set()

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        rows = db.execute(
            select(WebinarVideoJob.zoom_recording_uuid).where(
                WebinarVideoJob.zoom_recording_uuid.in_(candidates)
            )
        ).scalars()
        return set(rows)
    finally:
        db.close()
