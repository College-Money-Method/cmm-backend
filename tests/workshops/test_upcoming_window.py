"""A workshop stays registerable until it ends, and not a moment longer.

Two bugs are pinned here, pulling in opposite directions.

The first: a webinar flipped to "past" the moment its start time arrived, which
moved it into the portal's Previous Workshops list and took the registration
CTA down with it — parents arriving minutes after 7:00 PM found no way into a
session that was still running.

The second: the window that fixed it ran to midnight Pacific, hours past the
end. Zoom refuses a registration once the webinar is over (``code 3038``), and
``register_webinar`` is non-fatal, so those parents got a success screen, a
committed row, and no join link. The cutoff may never outlast Zoom's own.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.workshops.upcoming_window import is_upcoming, upcoming_cutoff

# 7:00 PM Eastern on 14 Sep 2026 — the session families were locked out of —
# and its 90-minute end.
SEPT_14_7PM_ET = datetime(2026, 9, 14, 23, 0, tzinfo=timezone.utc)
SEPT_14_END = SEPT_14_7PM_ET + timedelta(minutes=90)


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=timezone.utc)


def test_cutoff_is_the_workshops_end():
    assert upcoming_cutoff(SEPT_14_7PM_ET, SEPT_14_END) == _utc(2026, 9, 15, 0, 30)


def test_cutoff_is_normalised_to_utc():
    """An end stored in another zone still yields the same instant."""
    eastern = SEPT_14_END.astimezone(timezone(timedelta(hours=-4)))
    assert upcoming_cutoff(SEPT_14_7PM_ET, eastern) == SEPT_14_END


@pytest.mark.parametrize(
    "now, expected",
    [
        (_utc(2026, 9, 10, 12, 0), True),    # days before
        (_utc(2026, 9, 14, 23, 5), True),    # five minutes in, mid-session
        (_utc(2026, 9, 15, 0, 29), True),    # one minute before the end
        (_utc(2026, 9, 15, 0, 30), False),   # the end
        (_utc(2026, 9, 15, 0, 31), False),   # past
    ],
)
def test_is_upcoming_holds_until_the_end(now: datetime, expected: bool):
    assert is_upcoming(SEPT_14_7PM_ET, SEPT_14_END, now=now) is expected


def test_registration_does_not_outlast_zooms_own_cutoff():
    """Zoom answers 3038 once the webinar is over; we must close no later.

    The hours between the end and midnight Pacific are exactly where a parent
    would have been shown a register button Zoom then refused.
    """
    just_after_end = SEPT_14_END + timedelta(minutes=1)
    assert is_upcoming(SEPT_14_7PM_ET, SEPT_14_END, now=just_after_end) is False


def test_undated_webinar_is_upcoming():
    """Scheduled but unannounced — filing it under past would hide it for good."""
    assert is_upcoming(None) is True


class TestMalformedEnd:
    """An end that is missing or not after the start falls back to the day.

    These rows are broken data, not open-ended sessions. The fallback keeps a
    live workshop reachable instead of collapsing its window to zero.
    """

    def test_missing_end_falls_back_to_midnight_pacific(self):
        # 4:00 PM PDT on the 14th -> 00:00 PDT on the 15th -> 07:00 UTC.
        assert upcoming_cutoff(SEPT_14_7PM_ET, None) == _utc(2026, 9, 15, 7, 0)

    def test_end_equal_to_start_falls_back(self):
        assert upcoming_cutoff(SEPT_14_7PM_ET, SEPT_14_7PM_ET) == _utc(2026, 9, 15, 7, 0)

    def test_fallback_follows_the_pacific_calendar_day_not_the_utc_one(self):
        # 9:00 PM PST on 5 Jan is already 6 Jan in UTC; the cutoff tracks Pacific.
        assert upcoming_cutoff(_utc(2026, 1, 6, 5, 0)) == _utc(2026, 1, 6, 8, 0)

    def test_fallback_follows_dst(self):
        assert upcoming_cutoff(_utc(2026, 7, 15, 23, 0)) == _utc(2026, 7, 16, 7, 0)
        assert upcoming_cutoff(_utc(2026, 12, 15, 23, 0)) == _utc(2026, 12, 16, 8, 0)
