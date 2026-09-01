"""The timezone a school's workshop dates and times are written in.

Workshop datetimes are stored in UTC (``Webinar.start_datetime`` is a
``TIMESTAMP(timezone=True)``, fed from Airtable's ISO-8601 strings). UTC is the
right thing to *store* and the wrong thing to *show*: a 7:00 PM Eastern workshop
is 00:00 the following day in UTC, so an email rendered straight off the stored
value gets both ``{{time}}`` and ``{{date}}`` wrong.

Resolution order, most specific first:

1. ``School.display_timezone`` — set per school by an admin.
2. ``settings.workshop_display_timezone`` — the app-wide default, for the many
   schools that never set one.
3. ``FALLBACK_TIMEZONE`` — only if the setting itself is an unknown zone name,
   so a typo in the env degrades to a sane US zone rather than crashing a send.

The frontend has a byte-for-byte counterpart of ``US_TIMEZONES`` and of the
default in ``app/lib/us-timezones.ts`` (cmm-frontend). Both must agree, or the
Hub preview of a workshop email will disagree with what actually gets sent.
"""

from __future__ import annotations

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.config import settings

# Last-resort zone when the configured default is unusable. Not a policy
# choice — just somewhere real to land instead of raising mid-send.
FALLBACK_TIMEZONE = "America/New_York"

# The zones an admin can pick from. Every CMM workshop runs in the US, so this
# is deliberately the US list rather than the full IANA database — a short
# select beats a 600-entry one, and it doubles as the API's validation set.
US_TIMEZONES: tuple[tuple[str, str], ...] = (
    ("America/New_York", "Eastern"),
    ("America/Chicago", "Central"),
    ("America/Denver", "Mountain"),
    ("America/Phoenix", "Arizona (no DST)"),
    ("America/Los_Angeles", "Pacific"),
    ("America/Anchorage", "Alaska"),
    ("Pacific/Honolulu", "Hawaii"),
)

TIMEZONE_NAMES = frozenset(name for name, _label in US_TIMEZONES)


def is_supported_timezone(name: str) -> bool:
    """True when ``name`` is one an admin is allowed to choose."""
    return name in TIMEZONE_NAMES


def resolve_display_timezone(school_timezone: str | None) -> ZoneInfo:
    """The tzinfo to render a school's workshop dates in.

    Accepts any loadable IANA name, not just ``US_TIMEZONES`` — the picker
    constrains what gets *written*, and a row that predates (or outlives) that
    list should still render in the zone it actually names.
    """
    for candidate in (school_timezone, settings.workshop_display_timezone, FALLBACK_TIMEZONE):
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo(FALLBACK_TIMEZONE)
