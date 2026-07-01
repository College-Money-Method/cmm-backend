"""Attendance sync service — fetches post-webinar participant data from Zoom and updates DB."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.integrations import zoom as zoom_client
from src.workshops.models import Webinar, WorkshopRegistration

logger = logging.getLogger(__name__)


def sync_webinar_attendance(zoom_webinar_id: str, db: Session) -> bool:
    """
    Fetch participant report from Zoom and update WorkshopRegistration records.

    Matches participants to registrations by ``zoom_registrant_id`` first,
    falling back to email address. Sets ``attended``, ``join_time``,
    ``leave_time`` on each matched record and stamps ``attendance_synced_at``
    on the Webinar row.

    Returns True if participants were fetched and DB was updated, False if
    the Zoom report is not yet available (caller should retry later).
    """
    participants = zoom_client.get_webinar_participants(zoom_webinar_id)
    if participants is None:
        logger.warning("Attendance sync skipped — report unavailable for webinar=%s", zoom_webinar_id)
        return False

    webinar = db.scalars(
        select(Webinar).where(Webinar.zoom_webinar_id == zoom_webinar_id)
    ).first()
    if not webinar:
        logger.warning("Attendance sync skipped — no DB record for zoom_webinar_id=%s", zoom_webinar_id)
        return False

    registrations = db.scalars(
        select(WorkshopRegistration).where(WorkshopRegistration.webinar_id == webinar.id)
    ).all()

    # Build lookup maps for fast matching
    by_registrant_id: dict[str, WorkshopRegistration] = {
        r.zoom_registrant_id: r for r in registrations if r.zoom_registrant_id
    }
    by_email: dict[str, WorkshopRegistration] = {
        r.email.lower(): r for r in registrations
    }

    attended_ids: set = set()

    for p in participants:
        reg = by_registrant_id.get(p.get("registrant_id", "")) or by_email.get(
            (p.get("user_email") or "").lower()
        )
        if not reg:
            continue

        reg.attended = True
        attended_ids.add(reg.id)

        raw_join = p.get("join_time")
        raw_leave = p.get("leave_time")
        if raw_join:
            try:
                reg.join_time = datetime.fromisoformat(raw_join.replace("Z", "+00:00"))
            except ValueError:
                pass
        if raw_leave:
            try:
                reg.leave_time = datetime.fromisoformat(raw_leave.replace("Z", "+00:00"))
            except ValueError:
                pass

    # Explicitly mark non-attendees so the flag is accurate even after re-sync
    for reg in registrations:
        if reg.id not in attended_ids:
            reg.attended = False

    webinar.attendance_synced_at = datetime.now(tz=timezone.utc)
    db.commit()

    logger.info(
        "Attendance synced — webinar=%s attendees=%d/%d",
        zoom_webinar_id,
        len(attended_ids),
        len(registrations),
    )
    return True
