"""Validation for Paul's weekly bookable hours (stored on ``app_config``)."""

from __future__ import annotations

import re
from datetime import time

from src.paul_booking.rules import WEEKDAYS

_HHMM = re.compile(r"^([01]\d|2[0-3]):([03]0)$")


def _parse(value: object, label: str) -> time:
    match = _HHMM.match(value) if isinstance(value, str) else None
    if not match:
        raise ValueError(f"{label} must be HH:MM on the half hour (e.g. 09:00 or 13:30)")
    return time(int(match.group(1)), int(match.group(2)))


def validate_weekly_hours(value: dict | None) -> dict | None:
    """Check keys, grid and ordering; return a normalised copy.

    One range per weekday, ``null`` (or a missing day) meaning no calls that day.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Weekly hours must be an object keyed by weekday")
    cleaned: dict[str, dict[str, str] | None] = {}
    for day, hours in value.items():
        if day not in WEEKDAYS:
            raise ValueError(f"Unknown weekday {day!r}; use {', '.join(WEEKDAYS)}")
        if hours is None:
            cleaned[day] = None
            continue
        if not isinstance(hours, dict):
            raise ValueError(f"{day}: expected {{start, end}} or null")
        start = _parse(hours.get("start"), f"{day} start")
        end = _parse(hours.get("end"), f"{day} end")
        if start >= end:
            raise ValueError(f"{day}: start must be before end")
        cleaned[day] = {"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")}
    return cleaned


def day_range(weekly_hours: dict | None, weekday: str) -> tuple[time, time] | None:
    """Paul's open range for a weekday key (mon..sun), or None for a day off."""
    entry = (weekly_hours or {}).get(weekday)
    if not entry:
        return None
    return _parse(entry["start"], "start"), _parse(entry["end"], "end")
