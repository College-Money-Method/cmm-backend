"""A minimal iCalendar (.ics) builder for one event, no dependencies.

Just enough of RFC 5545 for a calendar app to import the invite: UTC times, text
escaping, CRLF line ends and 75-octet line folding.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ICS_MIME = "text/calendar"


def _escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,")
        .replace("\r\n", "\\n").replace("\n", "\\n").replace("\r", "\\n")
    )


def _fold(line: str) -> list[str]:
    """Split a long line into <=75-octet physical lines, continuation prefixed by a space."""
    out: list[str] = []
    current = ""
    limit = 75
    for ch in line:
        if len((current + ch).encode("utf-8")) > limit:
            out.append(current)
            current, limit = " " + ch, 75
        else:
            current += ch
    out.append(current)
    return out


def _utc(value: datetime) -> str:
    aware = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return aware.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_ics(
    *,
    uid: str,
    start: datetime,
    duration_minutes: int,
    summary: str,
    description: str,
    location: str,
    organizer_email: str,
    now: datetime,
) -> bytes:
    """One VEVENT with METHOD:PUBLISH. ``uid`` is stable so a re-sent invite
    updates the same calendar entry instead of duplicating it."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//College Money Method//Paul Booking//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:{uid}@collegemoneymethod.com",
        f"DTSTAMP:{_utc(now)}",
        f"DTSTART:{_utc(start)}",
        f"DTEND:{_utc(start + timedelta(minutes=duration_minutes))}",
        f"SUMMARY:{_escape(summary)}",
        f"DESCRIPTION:{_escape(description)}",
        f"LOCATION:{_escape(location)}",
        f"URL:{location}",
        f"ORGANIZER:mailto:{organizer_email}",
        "STATUS:CONFIRMED",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return ("\r\n".join(p for line in lines for p in _fold(line)) + "\r\n").encode("utf-8")
