"""Fixed booking rules for calls with Paul.

User-decided, deliberately not configurable: changing one is a code change so
the emails, the availability maths and the holds can never disagree about it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

# Earliest bookable moment, measured from now.
MIN_NOTICE = timedelta(hours=24)
# Furthest bookable moment, measured from now.
HORIZON = timedelta(days=21)
# How long Paul has to answer before an unanswered request lapses...
HOLD = timedelta(hours=48)
# ...or this long before the call starts, whichever comes first.
RELEASE_BEFORE_START = timedelta(hours=12)
# The scheduling grid: a 30-minute call is one block, a 60-minute call two.
BLOCK = timedelta(minutes=30)
# An "approving" claim older than this is treated as a crashed worker's.
APPROVE_CLAIM_TTL = timedelta(minutes=2)

DURATIONS = (30, 60)
DEFAULT_TIMEZONE = "America/Los_Angeles"
# Who gets request and confirmation emails when Global Settings names no one.
DEFAULT_NOTIFY_EMAIL = "paul.martin@collegemoneymethod.com"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def as_aware(value: datetime) -> datetime:
    """UTC-aware view of a datetime. SQLite (tests) hands timestamps back naive."""
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def expires_at_for(created_at: datetime, start_at: datetime) -> datetime:
    """When an unanswered request lapses.

    With 24h minimum notice this is always at least 12h after the request.
    """
    return min(created_at + HOLD, start_at - RELEASE_BEFORE_START)


def block_starts(start_at: datetime, duration_minutes: int) -> list[datetime]:
    """The 30-minute blocks a call occupies, in order."""
    return [start_at + BLOCK * i for i in range(duration_minutes // 30)]
