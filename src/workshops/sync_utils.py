"""Shared Airtable field-parsing helpers for workshop/webinar sync."""
from __future__ import annotations

from datetime import datetime


def attachment_url(val: object) -> str | None:
    """Extract the URL from an Airtable attachment field (returns a list of dicts)."""
    if isinstance(val, list) and val:
        return val[0].get("url") or None
    return None


def parse_airtable_datetime(val: object) -> datetime | None:
    """Parse an Airtable ISO-8601 datetime string to an aware datetime.

    Airtable can return a non-string (e.g. an ``{"error": "#ERROR!"}`` dict from a
    failing formula/computed field) instead of a date string — ignore those.
    """
    if not isinstance(val, str) or not val:
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except ValueError:
        return None
