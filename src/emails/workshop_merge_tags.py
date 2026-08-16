"""Python port of the frontend's ``buildWorkshopMergeReplacements``
(``app/lib/workshop-merge-tag-replacements.ts`` in cmm-frontend).

Kept as a straight line-by-line port (same tag set, same URL construction, same
grade-label pluralization) so a workshop email rendered server-side by the
automation scheduler (``scheduler.py``) matches what a counselor previews in
the Hub. Only the date/time formatting differs by necessity: the frontend
calls ``toLocaleDateString``/``toLocaleTimeString`` with no explicit
``timeZone``, which renders in the *viewer's* browser-local timezone — there is
no equivalent "viewer" for a server-rendered batch send, so this module
formats using whatever tzinfo the caller's ``start_datetime`` already carries
(expected: UTC, per the ``TIMESTAMP(timezone=True)`` column type). Documented
here rather than silently guessed at.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime


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
    school_slug: str | None,
    workshop_name: str,
    webinar_id: uuid.UUID | str,
    start_datetime: datetime | None,
    suggested_grades: str | None,
    cycle_name: str | None,
    resource_center_url: str | None = None,
    resource_center_password: str | None = None,
    registration_url: str | None = None,
    resources: list[dict] | None = None,
    origin: str | None = None,
) -> dict[str, str]:
    """Builds the full ``{{tag}}`` -> value map for all ``WORKSHOP_MERGE_TAGS``.

    ``resources`` is a list of ``{"id": str, "name": str, "link": str | None}``
    dicts (the Python-side shape of ``ContentAssetSummary``).

    ``recording_link`` and ``workshop_detail_url`` both resolve to the
    workshop detail page (not the raw Zoom URL) — same convention as the
    frontend, so families view the embedded recording/resources in-portal.
    """
    resolved_origin = origin or ""
    slug = workshop_slug(workshop_name, webinar_id)
    workshop_page_path = f"/school/{school_slug}/workshops/{slug}?via=email" if school_slug else None
    workshop_page_url = (
        f"{resolved_origin}{workshop_page_path}" if workshop_page_path else (registration_url or "")
    )

    resource_lines = []
    for r in _unique_resources(resources or []):
        detail_url = (
            f"{resolved_origin}/school/{school_slug}/resources/{r['id']}?via=email"
            if school_slug
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
        "resource_center_url": resource_center_url or "",
        "resource_center_password": resource_center_password or "",
        "workshop_name": workshop_name,
        "date": _format_date(start_datetime),
        "time": _format_time(start_datetime),
        "grade_label": _grade_label(suggested_grades),
        "registration_link": workshop_page_url,
        "cycle_name": cycle_name or "",
        "recording_link": workshop_page_url,
        "workshop_detail_url": workshop_page_url,
        "resources_list": "\n".join(resource_lines),
    }
