"""The name a published replay carries on Vimeo.

A webinar's own ``webinar_name`` is an operations label — it says which sitting
of a workshop this was, in whatever wording the person who scheduled it used.
The library it lands in is browsed by people looking for a workshop, on a date,
for a cohort, so the title is assembled from those instead:

    Workshop #1 Navigating the New System of College Pricing and Financial Aid
    November 17 2025 MOUNT Schools

Every part is optional at the source — a workshop may have no sequence number, a
webinar no cohort or no start time — so each is skipped rather than rendered as
a gap or a placeholder. A run with no webinar at all (an audit run started from
a paste) keeps the fallback it has always had.

It lives here rather than in ``process_recording`` because two processes need
the same string: the ECS task names the upload, and the API names the "ready"
email hours later.
"""

from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from src.config import settings
from src.video_pipeline.models import WebinarVideoJob

logger = logging.getLogger(__name__)

# Vimeo caps a video's name. Trimming here rather than letting the API decide
# keeps the cut somewhere readable and keeps a long workshop name from failing
# an otherwise finished upload.
MAX_TITLE = 128

AUDIT_PREFIX = "[Audit] "


def _display_date(when: datetime | None) -> str:
    """``November 17 2025`` in the timezone the workshops are advertised in.

    A webinar at 7pm Eastern is stored as the next day in UTC, so formatting the
    stored value directly would date a third of the library one day late. A
    naive datetime is assumed to already be local time — it carries no offset to
    convert from, and guessing UTC would shift a correct value by hours.
    """
    if when is None:
        return ""
    if when.tzinfo is not None:
        try:
            when = when.astimezone(ZoneInfo(settings.workshop_display_timezone))
        except Exception:  # pragma: no cover - unresolvable zone name
            logger.warning("Unknown display timezone %r — dating in UTC", settings.workshop_display_timezone)
    return f"{when:%B} {when.day} {when.year}"


def _parts(job: WebinarVideoJob) -> list[str]:
    """The title's pieces in order, with whatever is missing left out."""
    webinar = job.webinar
    if webinar is None:
        return []

    workshop = getattr(webinar, "workshop", None)
    cohort = getattr(webinar, "cohort", None)

    parts: list[str] = []
    sequence = getattr(workshop, "sequence_number", None)
    if sequence is not None:
        parts.append(f"Workshop #{sequence}")
    name = (getattr(workshop, "name", None) or "").strip()
    if name:
        parts.append(name)
    date = _display_date(getattr(webinar, "start_datetime", None))
    if date:
        parts.append(date)
    cohort_name = (getattr(cohort, "name", None) or "").strip()
    if cohort_name:
        parts.append(f"{cohort_name} Schools")
    return parts


def video_title(job: WebinarVideoJob) -> str:
    """Name the Vimeo video gets.

    An audit run is labelled even though its folder already separates it: the
    folder is a property of where the video sits, and a video that is later
    moved or shared out of it would otherwise be indistinguishable from a
    replay a school is watching.
    """
    parts = _parts(job)
    if parts:
        title = " ".join(parts)
    else:
        webinar = job.webinar
        title = (getattr(webinar, "webinar_name", None) if webinar else None) or (
            f"Webinar replay {job.id}"
        )

    limit = MAX_TITLE - (len(AUDIT_PREFIX) if job.audit_only else 0)
    if len(title) > limit:
        title = title[:limit].rstrip()
    return f"{AUDIT_PREFIX}{title}" if job.audit_only else title


__all__ = ["MAX_TITLE", "video_title"]
