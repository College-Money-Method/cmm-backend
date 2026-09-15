"""A workshop stays registerable until midnight Pacific on the day it runs.

The bug these pin down: a webinar flipped to "past" the moment its start time
arrived, which moved it into the portal's Previous Workshops list and took the
registration CTA down with it — parents arriving minutes after 7:00 PM found no
way into a session that was still running.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.workshops.upcoming_window import is_upcoming, upcoming_cutoff

# 7:00 PM Eastern on 14 Sep 2026 — the session families were locked out of.
SEPT_14_7PM_ET = datetime(2026, 9, 14, 23, 0, tzinfo=timezone.utc)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_cutoff_is_midnight_pacific_after_the_workshop_day():
    # 4:00 PM PDT on the 14th → 00:00 PDT on the 15th → 07:00 UTC.
    assert upcoming_cutoff(SEPT_14_7PM_ET) == _utc(2026, 9, 15, 7, 0)


def test_cutoff_follows_the_pacific_calendar_day_not_the_utc_one():
    # 9:00 PM PST on 5 Jan is already 6 Jan in UTC; the cutoff tracks Pacific.
    assert upcoming_cutoff(_utc(2026, 1, 6, 5, 0)) == _utc(2026, 1, 6, 8, 0)


def test_cutoff_follows_dst():
    assert upcoming_cutoff(_utc(2026, 7, 15, 23, 0)) == _utc(2026, 7, 16, 7, 0)
    assert upcoming_cutoff(_utc(2026, 12, 15, 23, 0)) == _utc(2026, 12, 16, 8, 0)


@pytest.mark.parametrize(
    "now, expected",
    [
        (_utc(2026, 9, 10, 12, 0), True),   # days before
        (_utc(2026, 9, 14, 23, 5), True),   # five minutes in, mid-session
        (_utc(2026, 9, 15, 0, 30), True),   # after the session ends
        (_utc(2026, 9, 15, 6, 59), True),   # 11:59 PM Pacific
        (_utc(2026, 9, 15, 7, 0), False),   # midnight Pacific
        (_utc(2026, 9, 15, 7, 1), False),   # past
    ],
)
def test_is_upcoming_holds_until_midnight_pacific(now: datetime, expected: bool):
    assert is_upcoming(SEPT_14_7PM_ET, now=now) is expected


def test_undated_webinar_is_upcoming():
    """Scheduled but unannounced — filing it under past would hide it for good."""
    assert is_upcoming(None) is True
