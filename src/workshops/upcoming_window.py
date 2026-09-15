"""How long a workshop still counts as "upcoming".

A webinar used to flip to "past" the instant its start time arrived. Families
who opened the school portal at 7:05 PM for a 7:00 PM workshop found it filed
under "Previous Workshops" with no way to register — the registration CTA is
only rendered for upcoming workshops — so late arrivals were locked out of a
session that was still running.

The rule instead: a workshop stays upcoming until midnight at the end of the
day it runs, read in Pacific time. Pacific is the latest zone any CMM family
sits in, so a cutoff expressed there is never earlier than the local midnight
of the family reading the page. The window always covers the whole session,
because a workshop that starts on a Pacific day also ends on it.

Deliberately NOT the school's display timezone: that would end registration at
9:00 PM Pacific for an Eastern school, which is the very lockout this fixes.

``app/lib/workshop-upcoming.ts`` in cmm-frontend is the byte-for-byte
counterpart of this rule. Both must agree, or a workshop the API returns under
``upcoming`` will render its detail page as a past recording.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, text
from sqlalchemy.sql.elements import ColumnClause

# The zone the cutoff is read in. A name, not a fixed offset, so DST is handled.
UPCOMING_CUTOFF_TIMEZONE = "America/Los_Angeles"

_CUTOFF_TZ = ZoneInfo(UPCOMING_CUTOFF_TIMEZONE)


def upcoming_cutoff(start: datetime) -> datetime:
    """The UTC instant ``start``'s workshop stops being upcoming.

    Midnight Pacific at the end of the workshop's Pacific calendar day.
    """
    local = start.astimezone(_CUTOFF_TZ)
    next_midnight = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=_CUTOFF_TZ)
    return next_midnight.astimezone(timezone.utc)


def is_upcoming(start: datetime | None, *, now: datetime | None = None) -> bool:
    """Whether a webinar starting at ``start`` still counts as upcoming.

    A webinar with no date is upcoming: it is scheduled but unannounced, and
    burying it under past recordings would hide it for good.
    """
    if start is None:
        return True
    return (now or datetime.now(tz=timezone.utc)) < upcoming_cutoff(start)


def _pacific_wall_clock(column: ColumnElement[datetime]) -> ColumnElement[datetime]:
    """``column`` as a naive timestamp reading in Pacific wall-clock time."""
    return func.timezone(UPCOMING_CUTOFF_TIMEZONE, column)


def is_upcoming_sql(start_column: ColumnClause[datetime]) -> ColumnElement[bool]:
    """``is_upcoming`` as a SQL predicate, for filtering a query by status.

    Compared entirely in Pacific wall-clock space: both sides of the comparison
    are converted out of UTC, so the day boundary is the Pacific one.
    """
    cutoff = func.date_trunc("day", _pacific_wall_clock(start_column)) + text("interval '1 day'")
    return (start_column.is_(None)) | (cutoff > _pacific_wall_clock(func.now()))
