"""The timezone workshop dates and times are written in.

Workshop datetimes are stored in UTC (``Webinar.start_datetime`` is a
``TIMESTAMP(timezone=True)``, fed from Airtable's ISO-8601 strings). UTC is the
right thing to *store* and the wrong thing to *show*: a 7:00 PM Eastern workshop
is 00:00 the following day in UTC, so an email rendered straight off the stored
value gets both ``{{time}}`` and ``{{date}}`` wrong.

One zone applies to every school: workshops all run on the same US schedule,
and the rendered time carries its abbreviation ("7:00 PM EDT"), so a family in
any state reads it unambiguously. Resolution order:

1. ``AppConfig.workshop_display_timezone`` — set by an admin in Global Settings.
2. ``settings.workshop_display_timezone`` — the env seed, used until an admin
   sets the app-wide default (and whenever the config row cannot be read).
3. ``FALLBACK_TIMEZONE`` — only if every candidate above is an unknown zone
   name, so a typo degrades to a sane US zone rather than crashing a send.

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


def resolve_display_timezone() -> ZoneInfo:
    """The tzinfo to render workshop dates in.

    Accepts any loadable IANA name, not just ``US_TIMEZONES`` — the picker
    constrains what gets *written*, and a value that predates (or outlives) that
    list should still render in the zone it actually names.
    """
    candidates = (
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
