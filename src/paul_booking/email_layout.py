"""Small helpers shared by the booking emails: local time text, links, HTML shell."""

from __future__ import annotations

from datetime import datetime
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.emails.school_links import email_origin
from src.paul_booking.rules import as_aware


def zone_or_utc(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ZoneInfo("UTC")


def when_text(moment: datetime, tz: ZoneInfo, duration_minutes: int | None = None) -> str:
    """e.g. ``Tuesday, October 20, 2026 at 9:00 AM PDT (30 min)``."""
    local = as_aware(moment).astimezone(tz)
    text = f"{local:%A, %B} {local.day}, {local.year} at {local:%I:%M %p}".replace(" 0", " ") + f" {local:%Z}"
    return f"{text} ({duration_minutes} min)" if duration_minutes else text


def site_link(path: str) -> str:
    return f"{email_origin()}{path}"


def google_calendar_url(*, title: str, start: datetime, end: datetime, details: str, location: str) -> str:
    def fmt(value: datetime) -> str:
        return as_aware(value).strftime("%Y%m%dT%H%M%SZ")

    return (
        "https://calendar.google.com/calendar/render?action=TEMPLATE"
        f"&text={quote(title)}&dates={fmt(start)}/{fmt(end)}"
        f"&details={quote(details)}&location={quote(location)}"
    )


def button(url: str, label: str) -> str:
    return (
        f'<p><a href="{escape(url, quote=True)}" style="background:#1d4ed8;color:#fff;padding:10px 18px;'
        f'border-radius:6px;text-decoration:none;display:inline-block">{escape(label)}</a></p>'
    )


def shell(heading: str, body_html: str) -> str:
    return (
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.5;color:#111827">'
        f'<h2 style="margin:0 0 12px">{escape(heading)}</h2>{body_html}'
        '<p style="color:#6b7280;font-size:12px;margin-top:24px">College Money Method</p></div>'
    )


def para(text: str) -> str:
    return f"<p>{escape(text)}</p>"
