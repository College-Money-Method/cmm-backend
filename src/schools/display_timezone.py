"""The timezone workshop dates and times are written in.

Workshop datetimes are stored in UTC (``Webinar.start_datetime`` is a
``TIMESTAMP(timezone=True)``, fed from Airtable's ISO-8601 strings). UTC is the
right thing to *store* and the wrong thing to *show*: a 7:00 PM Eastern workshop
is 00:00 the following day in UTC, so an email rendered straight off the stored
value gets both ``{{time}}`` and ``{{date}}`` wrong.

A workshop is one national Zoom event at one instant, but it is *advertised*
in the recipient school's own zone: the same webinar reads "7:00 PM EDT" to a
New York school and "4:00 PM PDT" to a California one. Same moment, and the
family does not have to do the arithmetic. Resolution order:

1. ``School.display_timezone`` — an admin's explicit per-school override.
2. The school's ``state``, through ``STATE_TIMEZONES``.
3. ``AppConfig.workshop_display_timezone`` — set by an admin in Global Settings.
4. ``settings.workshop_display_timezone`` — the env seed, used until an admin
   sets the app-wide default (and whenever the config row cannot be read).
5. ``FALLBACK_TIMEZONE`` — only if every candidate above is an unknown zone
   name, so a typo degrades to a sane US zone rather than crashing a send.

Steps 1 and 2 apply to *emails only*. Admin screens (webinar detail, the
workshops table, the Hub calendar) still print the app-wide zone, so a single
reference time exists for comparing schools; those call sites pass no school.

A counselor may pick their own zone for the Hub (``Contact.timezone``), but that
is a display preference for one person's screen and never reaches an email.

The frontend has a byte-for-byte counterpart of ``US_TIMEZONES`` and of the
default in ``app/lib/us-timezones.ts`` (cmm-frontend). Both must agree, or the
Hub preview of a workshop email will disagree with what actually gets sent.
"""

from __future__ import annotations

import time
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AfterValidator

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


def validate_display_timezone(value: str | None) -> str | None:
    """Reject a zone that is not on the supported list.

    Blank clears the override (back to the next fallback) rather than storing an
    empty string that would fail to load as a zone at send time.
    """
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    if not is_supported_timezone(cleaned):
        raise ValueError(f"Unsupported timezone: {cleaned}")
    return cleaned


# Writable timezone field: validated on the way in, so a send never has to cope
# with a zone name the picker could not have produced.
DisplayTimezoneField = Annotated[str | None, AfterValidator(validate_display_timezone)]


# The app-wide default lives in the database so an admin can change it without a
# deploy, but it is read on every rendered workshop email. Cached in-process so
# a bulk send does one query rather than one per recipient; the window is short
# enough that a change lands everywhere within minutes, and the process that
# made the change clears its own cache immediately.
_APP_DEFAULT_TTL_SECONDS = 300
_app_default_cache: tuple[float, str | None] | None = None


def reset_app_default_timezone_cache() -> None:
    """Drop the cached app-wide default — called when it is edited."""
    global _app_default_cache
    _app_default_cache = None


def app_default_timezone() -> str | None:
    """The admin-set app-wide default zone, or None if unset or unreadable.

    Never raises: a database that is down or a config row that does not exist
    yet must not take an email send with it — the caller falls through to the
    env seed.
    """
    global _app_default_cache
    now = time.monotonic()
    if _app_default_cache and now - _app_default_cache[0] < _APP_DEFAULT_TTL_SECONDS:
        return _app_default_cache[1]

    value: str | None = None
    try:
        # Imported here: src.app_config imports the ORM base, and this module is
        # pulled in by schema definitions that load before the app is wired up.
        from sqlalchemy import select

        from src.app_config.models import AppConfig
        from src.db.base import get_session_factory

        with get_session_factory()() as db:
            value = db.scalar(select(AppConfig.workshop_display_timezone))
    except Exception:  # noqa: BLE001 - see docstring
        value = None

    _app_default_cache = (now, value)
    return value


# The zone each US state is mapped to, by where most of the state's population
# lives. Sixteen states are split by a zone boundary; for those this is a
# best guess that an explicit ``School.display_timezone`` is meant to correct.
#
# Tennessee is the sharpest case in the current roster: the map says Central
# (Nashville, Memphis) while Chattanooga and Knoxville are genuinely Eastern.
# Deriving from `city` instead would not help — that column is free text and
# already holds typos ("San Antonioi") — and `zip_code` holds values that are
# not the school's ZIP at all, so a wrong-but-loadable zone would be picked
# silently. The state is a validated two-character column; the override is
# where a boundary school gets corrected.
STATE_TIMEZONES: dict[str, str] = {
    "AK": "America/Anchorage",
    "AL": "America/Chicago",
    "AR": "America/Chicago",
    "AZ": "America/Phoenix",
    "CA": "America/Los_Angeles",
    "CO": "America/Denver",
    "CT": "America/New_York",
    "DC": "America/New_York",
    "DE": "America/New_York",
    "FL": "America/New_York",   # split: the panhandle is Central
    "GA": "America/New_York",
    "HI": "Pacific/Honolulu",
    "IA": "America/Chicago",
    "ID": "America/Denver",     # split: the northern panhandle is Pacific
    "IL": "America/Chicago",
    "IN": "America/New_York",   # split: the NW and SW corners are Central
    "KS": "America/Chicago",    # split: four western counties are Mountain
    "KY": "America/New_York",   # split: the western third is Central
    "LA": "America/Chicago",
    "MA": "America/New_York",
    "MD": "America/New_York",
    "ME": "America/New_York",
    "MI": "America/New_York",   # split: four western UP counties are Central
    "MN": "America/Chicago",
    "MO": "America/Chicago",
    "MS": "America/Chicago",
    "MT": "America/Denver",
    "NC": "America/New_York",
    "ND": "America/Chicago",    # split: the southwest is Mountain
    "NE": "America/Chicago",    # split: the western panhandle is Mountain
    "NH": "America/New_York",
    "NJ": "America/New_York",
    "NM": "America/Denver",
    "NV": "America/Los_Angeles",
    "NY": "America/New_York",
    "OH": "America/New_York",
    "OK": "America/Chicago",    # split: the far western panhandle is Mountain
    "OR": "America/Los_Angeles",  # split: most of Malheur County is Mountain
    "PA": "America/New_York",
    "RI": "America/New_York",
    "SC": "America/New_York",
    "SD": "America/Chicago",    # split: the western half is Mountain
    "TN": "America/Chicago",    # split: the eastern third is Eastern
    "TX": "America/Chicago",    # split: El Paso and Hudspeth are Mountain
    "UT": "America/Denver",
    "VA": "America/New_York",
    "VT": "America/New_York",
    "WA": "America/Los_Angeles",
    "WI": "America/Chicago",
    "WV": "America/New_York",
    "WY": "America/Denver",
}


def timezone_for_state(state: str | None) -> str | None:
    """The zone a two-letter state code maps to, or None when unrecognised.

    Unknown input (a blank, a full state name, a territory outside the map)
    returns None rather than guessing, so the caller falls through to the
    app-wide default instead of advertising a workshop in the wrong hour.
    """
    if not state:
        return None
    return STATE_TIMEZONES.get(state.strip().upper())


def resolve_display_timezone(
    school_timezone: str | None = None,
    school_state: str | None = None,
) -> ZoneInfo:
    """The tzinfo to render workshop dates in for one school.

    Called with no arguments this is the app-wide zone — what admin screens and
    any non-school-specific render should use. Pass a school's override and
    state to get the zone its families actually read the time in.

    Accepts any loadable IANA name, not just ``US_TIMEZONES`` — the picker
    constrains what gets *written*, and a value that predates (or outlives) that
    list should still render in the zone it actually names.
    """
    candidates = (
        school_timezone,
        timezone_for_state(school_state),
        app_default_timezone(),
        settings.workshop_display_timezone,
        FALLBACK_TIMEZONE,
    )
    for candidate in candidates:
        if not candidate:
            continue
        try:
            return ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return ZoneInfo(FALLBACK_TIMEZONE)
