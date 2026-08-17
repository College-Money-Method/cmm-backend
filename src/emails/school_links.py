"""Canonical school-portal URLs used by merge tags.

One definition so the broadcast and workshop tag builders can never disagree
about where a school's resource center lives.
"""

from __future__ import annotations


def resource_center_url(origin: str | None, school_slug: str | None) -> str:
    """Absolute URL of a school's resource center — the school portal home.

    Computed from the portal slug rather than read off
    ``School.school_resource_center_url``: that column holds the legacy Airtable
    link, which points at the old site and goes stale as soon as a school is
    migrated. The counselor hub already derives the tag this way (see
    ``app/routes/hub/communications.tsx``), so emails now match what counselors
    see when they preview the same template.

    Empty string when the school has no slug — the tag then renders blank
    instead of linking somewhere wrong.
    """
    if not school_slug:
        return ""
    return f"{(origin or '').rstrip('/')}/school/{school_slug}"
