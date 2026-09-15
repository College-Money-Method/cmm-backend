"""How long a workshop still counts as "upcoming".

A webinar used to flip to "past" the instant its start time arrived. Families
who opened the school portal at 7:05 PM for a 7:00 PM workshop found it filed
under "Previous Workshops" with no way to register — the registration CTA is
only rendered for upcoming workshops — so late arrivals were locked out of a
session that was still running.

The rule: a workshop stays upcoming until it ends. The window covers the whole
session, so a parent arriving mid-session can still register, and it shuts the
moment the session is over.

The end is the boundary because it is also *Zoom's* boundary. Registering
against a finished webinar is refused outright::

    POST /webinars/{id}/registrants
    -> 400 {"code":3038,"message":"The webinar is over. You cannot register now."}

Zoom reads that from the scheduled end, not from when the host actually
wrapped up — sessions the host never started answer 3038 all the same. So any
window reaching past ``end_datetime`` would show a register button that Zoom
then refuses, and ``register_webinar`` is deliberately non-fatal: the row
commits, the parent sees a success screen, and no join link ever arrives. A
cutoff later than Zoom's own is therefore not a nicety, it is a silent failure.

``app/lib/workshop-upcoming.ts`` in cmm-frontend is the counterpart of this
rule. Both must agree, or a workshop the API returns under ``upcoming`` will
render its detail page as a past recording.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, case, func, text
from sqlalchemy.sql.elements import ColumnClause

# The zone the fallback cutoff is read in. A name, not a fixed offset, so DST
# is handled.
UPCOMING_CUTOFF_TIMEZONE = "America/Los_Angeles"

_CUTOFF_TZ = ZoneInfo(UPCOMING_CUTOFF_TIMEZONE)


def _midnight_after(start: datetime) -> datetime:
    """Midnight Pacific at the end of ``start``'s Pacific calendar day, in UTC.

    The fallback for a webinar whose end is missing or not after its start.
    Pacific is the latest zone any CMM family sits in, so the window is never
    shorter than the reader's own day.
    """
    local = start.astimezone(_CUTOFF_TZ)
    next_midnight = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=_CUTOFF_TZ)
    return next_midnight.astimezone(timezone.utc)


def upcoming_cutoff(start: datetime, end: datetime | None = None) -> datetime:
    """The UTC instant this workshop stops being upcoming.

    Its end, which is where Zoom stops accepting registrations. Rows whose end
    is missing or not after the start are malformed rather than open-ended, so
    they fall back to the end of the workshop's Pacific day.
    """
    if end is not None and end > start:
        return end.astimezone(timezone.utc)
    return _midnight_after(start)


def is_upcoming(
    start: datetime | None,
    end: datetime | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether a webinar running ``start``–``end`` still counts as upcoming.

    A webinar with no date is upcoming: it is scheduled but unannounced, and
    burying it under past recordings would hide it for good.
    """
    if start is None:
        return True
    return (now or datetime.now(tz=timezone.utc)) < upcoming_cutoff(start, end)


def _pacific_wall_clock(column: ColumnElement[datetime]) -> ColumnElement[datetime]:
    """``column`` as a naive timestamp reading in Pacific wall-clock time."""
    return func.timezone(UPCOMING_CUTOFF_TIMEZONE, column)


def is_upcoming_sql(
    start_column: ColumnClause[datetime],
    end_column: ColumnClause[datetime],
) -> ColumnElement[bool]:
    """``is_upcoming`` as a SQL predicate, for filtering a query by status."""
    # The fallback is compared entirely in Pacific wall-clock space, so the day
    # boundary is the Pacific one rather than UTC's.
    midnight = func.date_trunc("day", _pacific_wall_clock(start_column)) + text("interval '1 day'")
    return (start_column.is_(None)) | case(
        (end_column > start_column, end_column > func.now()),
        else_=midnight > _pacific_wall_clock(func.now()),
    )
