"""free_block_starts / slot_starts: the pure availability rules."""

from __future__ import annotations

from datetime import date, timedelta
from zoneinfo import ZoneInfo

from src.paul_booking.availability import free_block_starts, slot_starts
from src.paul_booking.hours import validate_weekly_hours
from src.paul_booking.rules import BLOCK
from tests.paul_booking.conftest import utc

PT = ZoneInfo("America/Los_Angeles")
# Tuesday 2026-10-20 12:00 UTC: far from any DST change, a clean reference clock.
NOW = utc(2026, 10, 20, 12, 0)


def blocks(hours, *, tz=PT, time_off=(), busy=(), held=(), now=NOW):
    return free_block_starts(hours, tz, time_off, busy, held, now)


def test_blocks_follow_weekly_hours_in_hosts_zone():
    # Thu 2026-10-22 09:00-10:30 PDT = 16:00-17:30Z.
    got = blocks({"thu": {"start": "09:00", "end": "10:30"}})[:3]
    assert got == [utc(2026, 10, 22, 16, 0), utc(2026, 10, 22, 16, 30), utc(2026, 10, 22, 17, 0)]


def test_min_notice_is_inclusive_at_24_hours():
    # Wed 2026-10-21 12:00Z is exactly now+24h = 05:00 PDT; 04:30 PDT is too soon.
    got = blocks({"wed": {"start": "04:30", "end": "06:00"}})
    assert got[0] == utc(2026, 10, 21, 12, 0)
    assert utc(2026, 10, 21, 11, 30) not in got


def test_horizon_ends_after_21_days():
    got = blocks({d: {"start": "00:00", "end": "23:30"} for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")})
    assert max(got) < NOW + timedelta(days=21)
    assert max(got) >= NOW + timedelta(days=21) - BLOCK


def test_day_off_when_weekday_null_or_missing():
    hours = {"thu": None, "fri": {"start": "09:00", "end": "10:00"}}
    got = blocks(hours)
    assert got and all(b.astimezone(PT).weekday() == 4 for b in got)


def test_time_off_removes_whole_days_inclusive():
    hours = {"thu": {"start": "09:00", "end": "10:00"}}
    # Thu Oct 22 off; Thu Oct 29 stays.
    got = blocks(hours, time_off=[(date(2026, 10, 22), date(2026, 10, 22))])
    assert [b.astimezone(PT).date() for b in got][0] == date(2026, 10, 29)
    # Inclusive end: a range ending on the 29th takes that Thursday too.
    got = blocks(hours, time_off=[(date(2026, 10, 20), date(2026, 10, 29))])
    assert got[0].astimezone(PT).date() == date(2026, 11, 5)


def test_busy_overlapping_block_edges_removes_both_blocks():
    hours = {"thu": {"start": "09:00", "end": "11:00"}}
    # 09:15-09:45 PDT touches the 09:00 and the 09:30 block, not 10:00.
    busy = [(utc(2026, 10, 22, 16, 15), utc(2026, 10, 22, 16, 45))]
    got = [b for b in blocks(hours, busy=busy) if b.date() == date(2026, 10, 22)]
    assert got == [utc(2026, 10, 22, 17, 0), utc(2026, 10, 22, 17, 30)]


def test_busy_ending_exactly_at_block_start_does_not_block_it():
    hours = {"thu": {"start": "09:00", "end": "10:00"}}
    busy = [(utc(2026, 10, 22, 15, 30), utc(2026, 10, 22, 16, 0))]
    assert utc(2026, 10, 22, 16, 0) in blocks(hours, busy=busy)


def test_held_blocks_are_unavailable():
    hours = {"thu": {"start": "09:00", "end": "10:00"}}
    got = blocks(hours, held=[utc(2026, 10, 22, 16, 0)])
    assert utc(2026, 10, 22, 16, 0) not in got and utc(2026, 10, 22, 16, 30) in got


def test_sixty_minute_needs_two_adjacent_free_blocks():
    hours = {"thu": {"start": "09:00", "end": "11:00"}}
    # 09:30 PDT held: 09:00 has no free neighbour, 10:00 does (10:00+10:30).
    free = blocks(hours, held=[utc(2026, 10, 22, 16, 30)])
    day = [b for b in free if b.date() == date(2026, 10, 22)]
    assert slot_starts(day, 30) == day
    assert slot_starts(day, 60) == [utc(2026, 10, 22, 17, 0)]


def test_dst_end_shifts_utc_but_not_local_hours():
    # DST ends Sun 2026-11-01 at 02:00 local. Saturday is PDT (UTC-7), Sunday PST (UTC-8).
    got = blocks({"sat": {"start": "09:00", "end": "10:00"}, "sun": {"start": "09:00", "end": "10:00"}})
    assert utc(2026, 10, 31, 16, 0) in got and utc(2026, 11, 1, 17, 0) in got
    assert utc(2026, 11, 1, 16, 0) not in got


def test_range_spanning_the_fall_back_has_the_extra_hour():
    # 00:00-04:00 local on the long night is five real hours = ten half hours.
    got = [b for b in blocks({"sun": {"start": "00:00", "end": "04:00"}}) if b.astimezone(PT).date() == date(2026, 11, 1)]
    assert len(got) == 10


def test_dst_start_loses_an_hour():
    # Spring forward 2027-03-14: 00:00-04:00 local is three real hours = six blocks.
    now = utc(2027, 3, 1, 12, 0)
    got = [b for b in blocks({"sun": {"start": "00:00", "end": "04:00"}}, now=now) if b.astimezone(PT).date() == date(2027, 3, 14)]
    assert len(got) == 6


def test_weekly_hours_validation():
    assert validate_weekly_hours({"mon": {"start": "09:00", "end": "17:30"}, "sat": None}) == {
        "mon": {"start": "09:00", "end": "17:30"}, "sat": None}
    for bad in (
        {"funday": {"start": "09:00", "end": "10:00"}},
        {"mon": {"start": "09:15", "end": "10:00"}},
        {"mon": {"start": "10:00", "end": "10:00"}},
        {"mon": {"start": "11:00", "end": "10:00"}},
        {"mon": {"start": "9:00", "end": "10:00"}},
        {"mon": "9-5"},
    ):
        try:
            validate_weekly_hours(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
