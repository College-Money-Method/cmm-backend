"""Pure availability maths: which 30-minute blocks Paul could take a call in.

No database, no network: callers pass in everything, so every rule (DST, notice,
horizon, days off, busy, holds) is testable with plain values.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.paul_booking.hours import day_range
from src.paul_booking.rules import BLOCK, HORIZON, MIN_NOTICE, WEEKDAYS, as_aware

Interval = tuple[datetime, datetime]


def _on_time_off(day: date, time_off: Iterable[tuple[date, date]]) -> bool:
    return any(starts <= day <= ends for starts, ends in time_off)


def free_block_starts(
    weekly_hours: dict | None,
    tz: ZoneInfo,
    time_off: Iterable[tuple[date, date]],
    busy: Iterable[Interval],
    held: Iterable[datetime],
    now: datetime,
) -> list[datetime]:
    """UTC start of every free 30-minute block in [now + 24h, now + 21d), sorted.

    A block is free when it sits inside that weekday's hours (Paul's zone), is not
    on a day off, does not overlap a busy Zoom interval, and is not a held block.
    Blocks are laid out in UTC from the local start of each day's range, so a DST
    change inside a range yields the real number of elapsed half hours.
    """
    now = as_aware(now)
    earliest, latest = now + MIN_NOTICE, now + HORIZON
    off = list(time_off)
    # Held blocks are 30-minute busy intervals like any other.
    taken = [(as_aware(s), as_aware(e)) for s, e in busy]
    taken += [(as_aware(h), as_aware(h) + BLOCK) for h in held]

    free: list[datetime] = []
    local_day = earliest.astimezone(tz).date() - timedelta(days=1)
    last_day = latest.astimezone(tz).date() + timedelta(days=1)
    while local_day <= last_day:
        hours = day_range(weekly_hours, WEEKDAYS[local_day.weekday()])
        if hours and not _on_time_off(local_day, off):
            start = datetime.combine(local_day, hours[0], tzinfo=tz).astimezone(timezone.utc)
            end = datetime.combine(local_day, hours[1], tzinfo=tz).astimezone(timezone.utc)
            block = start
            while block + BLOCK <= end:
                in_window = earliest <= block < latest
                if in_window and not any(b_start < block + BLOCK and b_end > block for b_start, b_end in taken):
                    free.append(block)
                block += BLOCK
        local_day += timedelta(days=1)
    return free


def slot_starts(free_blocks: Iterable[datetime], duration_minutes: int) -> list[datetime]:
    """Start times a call of this length can take: 30 = any free block, 60 = a
    free block whose following block is free too."""
    blocks = sorted(free_blocks)
    if duration_minutes == 30:
        return blocks
    available = set(blocks)
    return [b for b in blocks if b + BLOCK in available]
