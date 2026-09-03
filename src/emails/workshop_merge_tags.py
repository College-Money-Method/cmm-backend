"""Python port of the frontend's ``buildWorkshopMergeReplacements``
(``app/lib/workshop-merge-tag-replacements.ts`` in cmm-frontend).

Kept as a straight line-by-line port (same tag set, same URL construction, same
grade-label pluralization) so a workshop email rendered server-side by the
automation scheduler (``scheduler.py``) matches what a counselor previews in
the Hub.

Dates and times are rendered in the *school's* display timezone rather than in
the stored UTC (see ``src.schools.display_timezone``). Both repos resolve that
zone the same way — the school's override, then its state, then the app-wide
default — so the Hub preview and the sent email agree; neither uses the
viewer's browser zone, which has nothing to do with when the workshop actually
starts.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from src.emails.school_links import resource_center_url
from src.schools.display_timezone import resolve_display_timezone


def _slugify(text: str) -> str:
    """Mirrors ``app/lib/content-headings.ts::slugify`` exactly:
    lowercase, collapse non-alphanumeric runs to a single hyphen, trim edges.
    """
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    lowered = normalized.lower()
    collapsed = re.sub(r"[^a-z0-9]+", "-", lowered)
    return collapsed.strip("-")


def workshop_slug(name: str, webinar_id: uuid.UUID | str) -> str:
    """Mirrors ``app/lib/workshop-slug.ts::workshopSlug``: name-slug + first 8
    hex chars of the webinar UUID (dashes stripped) as an index-friendly suffix.
    """
    prefix = str(webinar_id).replace("-", "")[:8]
    return f"{_slugify(name)}-{prefix}"


def _in_display_tz(dt: datetime | None, tz: ZoneInfo) -> datetime | None:
    """``dt`` moved into the school's display zone.

    A naive datetime is assumed to already BE local time — it carries no offset
    to convert from, and guessing UTC would shift a correct value by hours.
    """
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(tz)


def _format_date(dt: datetime | None) -> str:
    if dt is None:
        return "TBD"
    return f"{dt.strftime('%A, %B')} {dt.day}, {dt.year}"


def _format_time(dt: datetime | None) -> str:
    if dt is None:
        return "TBD"
    hour_12 = dt.strftime("%I").lstrip("0") or "12"
    time_str = f"{hour_12}:{dt.strftime('%M %p')}"
    tz_label = dt.tzname()
    return f"{time_str} {tz_label}" if tz_label else time_str


def _grade_label(grades: str | None) -> str:
    if not grades:
        return ""
    parts: list[str] = []
    for raw in grades.split(","):
        raw = raw.strip()
        try:
            parts.append(f"{int(raw)}th")
        except ValueError:
            continue
    if not parts:
        return ""
    if len(parts) == 1:
        return f"{parts[0]} Grade Families"
    if len(parts) == 2:
        return f"{parts[0]} & {parts[1]} Grade Families"
    return f"{', '.join(parts[:-1])}, & {parts[-1]} Grade Families"


def _unique_resources(resources: list[dict]) -> list[dict]:
    """Dedupe by id (same asset can be attached to multiple objectives),
    preserving order — mirrors the TS ``uniqueResources`` helper."""
    seen: set[str] = set()
    out = []
    for r in resources:
        rid = r.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        out.append(r)
    return out


def build_workshop_merge_replacements(
    *,
    school_name: str,
    family_label: str,
    counselor_name: str,
    counselor_first_name: str = "",
    counselor_last_name: str = "",
    recipient_first_names: str = "",
    school_slug: str | None,
    school_state: str | None = None,
    school_timezone: str | None = None,
    workshop_name: str,
    webinar_id: uuid.UUID | str,
    start_datetime: datetime | None,
    suggested_grades: str | None,
    cycle_name: str | None,
    resource_center_password: str | None = None,
    registration_url: str | None = None,
    resources: list[dict] | None = None,
    origin: str | None = None,
    registration_count: int = 0,
    attendee_count: int = 0,
) -> dict[str, str]:
    """Builds the full ``{{tag}}`` -> value map for all ``WORKSHOP_MERGE_TAGS``.

    ``resources`` is a list of ``{"id": str, "name": str, "link": str | None}``
    dicts (the Python-side shape of ``ContentAssetSummary``).

    ``recording_link`` and ``workshop_detail_url`` both resolve to the
    workshop detail page (not the raw Zoom URL) — same convention as the
    frontend, so families view the embedded recording/resources in-portal.

    ``registration_count``/``attendee_count`` are this school's numbers for this
    webinar, counted by the caller at render time (``registrations_to_date`` is
    "as of now", so it is never a stored value).

    ``start_datetime`` is converted into the school's display zone before
    ``{{date}}``/``{{time}}`` are formatted, so an evening workshop does not
    advertise itself as the next day in UTC, and a family reads the hour they
    will actually join at. ``school_timezone`` is the school's explicit
    override and ``school_state`` the two-letter code the zone is otherwise
    derived from; with neither, this falls back to the app-wide default, which
    is what a preview with no school attached gets.

    ``recipient_first_names`` is the greeting name(s) for whoever this copy is
    addressed to, already joined by the caller (``broadcast_send.format_name_list``).
    Workshop automations send one email per contact, so it is that contact's
    first name there; the tag exists on this path so an automation template can
    open with "Hi {{recipient_first_names}}," the same way a broadcast does.
    """
    # An absolute origin is required: these values land in emails, where a
    # relative href resolves against nothing and renders as "http:///school/...".
    # With no origin we fall back to the Zoom registration URL (and to each
    # resource's own link) rather than emitting a path — a missing link is
    # recoverable, a hostless one is not.
    resolved_origin = (origin or "").rstrip("/")
    local_start = _in_display_tz(
        start_datetime, resolve_display_timezone(school_timezone, school_state)
    )
    slug = workshop_slug(workshop_name, webinar_id)
    workshop_page_path = (
        f"/school/{school_slug}/workshops/{slug}?via=email"
        if school_slug and resolved_origin
        else None
    )
    workshop_page_url = (
        f"{resolved_origin}{workshop_page_path}" if workshop_page_path else (registration_url or "")
    )

    resource_lines = []
    for r in _unique_resources(resources or []):
        detail_url = (
            f"{resolved_origin}/school/{school_slug}/resources/{r['id']}?via=email"
            if school_slug and resolved_origin
            else r.get("link")
        )
        name = r.get("name", "")
        resource_lines.append(f"- {name} ({detail_url})" if detail_url else f"- {name}")

    return {
        "school_name": school_name,
        "family_label": family_label,
        "counselor_name": counselor_name,
        "counselor_first_name": counselor_first_name,
        "counselor_last_name": counselor_last_name,
        "recipient_first_names": recipient_first_names,
        "resource_center_url": resource_center_url(resolved_origin, school_slug),
        "resource_center_password": resource_center_password or "",
        "workshop_name": workshop_name,
        "date": _format_date(local_start),
        "time": _format_time(local_start),
        "grade_label": _grade_label(suggested_grades),
        "registration_link": workshop_page_url,
        "cycle_name": cycle_name or "",
        "recording_link": workshop_page_url,
        "workshop_detail_url": workshop_page_url,
        "resources_list": "\n".join(resource_lines),
        "registrations_to_date": str(registration_count),
        "attendees": str(attendee_count),
    }
