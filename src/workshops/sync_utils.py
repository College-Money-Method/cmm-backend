"""Shared Airtable field-parsing helpers for workshop/webinar sync."""
from __future__ import annotations

from collections.abc import Hashable
from datetime import datetime


def select_stale_mapping_pairs(
    existing_pairs: list[Hashable],
    desired_pairs: set,
    max_missing_fraction: float,
) -> tuple[list, bool]:
    """Decide which portal-mapping pairs to remove during reconciliation.

    ``existing_pairs`` — (school_id, webinar_id) pairs currently in the DB for
    the webinars being reconciled. ``desired_pairs`` — the pairs Airtable still
    lists. Returns ``(stale_pairs, guard_tripped)``:

    - ``stale_pairs``: existing pairs Airtable no longer lists.
    - ``guard_tripped``: True when the stale fraction exceeds
      ``max_missing_fraction`` — a spike signals a bad/partial Airtable pull, so
      the caller must delete nothing. (False when nothing is stale.)
    """
    total = len(existing_pairs)
    stale = [p for p in existing_pairs if p not in desired_pairs]
    guard_tripped = bool(total) and bool(stale) and (len(stale) / total) > max_missing_fraction
    return stale, guard_tripped


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
