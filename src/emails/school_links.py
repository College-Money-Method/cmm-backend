"""Canonical school-portal URLs used by merge tags.

One definition so the broadcast and workshop tag builders can never disagree
about where a school's resource center lives — or about which origin those
links hang off.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def email_origin() -> str:
    """Absolute origin for links in outgoing email, without a trailing slash.

    There is no ``request`` at send time (unlike interactive routes), so the
    origin comes from settings. ``app_public_url`` wins; ``frontend_url`` is the
    fallback, matching the precedence ``build_preferences_url`` already used.

    Every email link builder must go through here. Previously each call site
    wrote ``settings.app_public_url or None`` and each one independently turned
    an empty origin into a relative href — which is unusable in an email client.

    Returns an empty string only if both settings are explicitly blanked, which
    callers must treat as "cannot build a link" rather than emitting a path.
    """
    # Imported lazily so tests can monkeypatch the settings freely without
    # import-order surprises (same reason as `renderer.render_email`).
    from src.config import settings

    origin = (settings.app_public_url or settings.frontend_url or "").strip().rstrip("/")
    if not origin:
        logger.error(
            "No email origin configured: both APP_PUBLIC_URL and FRONTEND_URL are empty. "
            "Links in outgoing email cannot be built and will be omitted."
        )
    return origin


def check_email_origin() -> None:
    """Log the origin email links will be built from, loudly if it looks wrong.

    Called at startup because a bad origin is invisible at send time: the link
    still renders, so nobody notices until a recipient reports a dead one —
    which is exactly how the hostless "http:///school/..." link reached
    production, with prod running for months with no APP_PUBLIC_URL set at all.
    """
    from src.config import settings

    origin = email_origin()
    if not origin:
        # email_origin() already logged the misconfiguration.
        return
    if settings.environment != "development" and "localhost" in origin:
        logger.error(
            "Email origin is %s in environment %r: outgoing email would link "
            "recipients at a local address. Set APP_PUBLIC_URL to the public site.",
            origin,
            settings.environment,
        )
        return
    logger.info("Email links will be built from origin %s", origin)


def resource_center_url(origin: str | None, school_slug: str | None) -> str:
    """Absolute URL of a school's resource center — the school portal home.

    Computed from the portal slug rather than read off
    ``School.school_resource_center_url``: that column holds the legacy Airtable
    link, which points at the old site and goes stale as soon as a school is
    migrated. The counselor hub already derives the tag this way (see
    ``app/routes/hub/communications.tsx``), so emails now match what counselors
    see when they preview the same template.

    Empty string when the school has no slug, or when no origin is configured —
    the tag then renders blank instead of linking somewhere wrong. A bare
    ``/school/<slug>`` path is never returned: mail clients resolve a relative
    href against nothing, producing "http:///school/<slug>".
    """
    if not school_slug:
        return ""
    base = (origin or "").rstrip("/")
    if not base:
        return ""
    return f"{base}/school/{school_slug}"
