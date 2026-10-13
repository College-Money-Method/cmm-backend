"""Renders the daily survey digest email sent to every super_admin.

Pure: takes the claimed responses plus pre-computed stats and returns
(subject, html, text). No DB or network access, so it is tested directly.

Every piece of respondent-supplied text (comment, resource name, page URL) is
HTML-escaped — feedback-widget submissions are anonymous and unauthenticated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from src.surveys.models import SurveyResponse

DIGEST_ITEM_CAP = 25
COMMENT_PREVIEW_CHARS = 300
DIGEST_TIMEZONE = ZoneInfo("America/Los_Angeles")


@dataclass(frozen=True)
class DigestStats:
    """All responses (actionable or not) in the period the digest covers."""

    period_label: str   # e.g. "last 24 hours", "since Friday"
    total: int
    thumbs_up: int
    thumbs_down: int
    avg_stars: float | None


def _rating_label(row: SurveyResponse) -> str:
    if row.rating_thumbs is False:
        return "👎"
    if row.rating_thumbs is True:
        return "👍"
    if row.rating_stars is not None:
        return f"{row.rating_stars}★"
    return "Comment"


def _comment_preview(comment: str | None) -> str | None:
    if not comment or not comment.strip():
        return None
    text = comment.strip()
    return text if len(text) <= COMMENT_PREVIEW_CHARS else text[:COMMENT_PREVIEW_CHARS].rstrip() + "…"


def _absolute_page_url(page_url: str, origin: str) -> str | None:
    """Rebuild the link on our own origin, keeping only path and query.

    ``page_url`` comes from the public survey POST, so its host is untrusted:
    re-rooting it means a "View page" link in an admin's inbox can never point
    at someone else's site, and non-http schemes (``javascript:``) are dropped.
    """
    if not origin:
        return None
    parts = urlsplit((page_url or "").strip())
    if parts.scheme not in ("", "http", "https") or not parts.path.startswith("/"):
        return None
    return origin.rstrip("/") + urlunsplit(("", "", parts.path, parts.query, ""))


def _context_line(row: SurveyResponse, school_names: dict[str, str]) -> str:
    parts = [row.page_type.replace("_", " ").title()]
    if row.resource_name:
        parts.append(row.resource_name)
    school = school_names.get(row.school_id or "")
    if school:
        parts.append(school)
    return " · ".join(parts)


def _when(created_at: datetime) -> str:
    # SQLite (tests) hands back naive timestamps; the column is stored as UTC.
    aware = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
    local = aware.astimezone(DIGEST_TIMEZONE)
    return local.strftime("%b %-d, %-I:%M %p PT")


def _stats_line(stats: DigestStats) -> str:
    parts = [f"{stats.total} response{'s' if stats.total != 1 else ''} {stats.period_label}"]
    if stats.thumbs_up or stats.thumbs_down:
        parts.append(f"{stats.thumbs_up} 👍 / {stats.thumbs_down} 👎")
    if stats.avg_stars is not None:
        parts.append(f"avg {stats.avg_stars:.1f}★")
    return " · ".join(parts)


def render_survey_digest(
    rows: Sequence[SurveyResponse],
    stats: DigestStats,
    school_names: dict[str, str],
    origin: str,
    today: datetime,
) -> tuple[str, str, str]:
    """Subject, HTML body and plain-text body for one digest.

    ``rows`` are the actionable responses this run claimed; the newest
    ``DIGEST_ITEM_CAP`` are listed and the rest summarised as "+N more".
    """
    count = len(rows)
    verb = "needs" if count == 1 else "need"
    subject = f"Survey feedback: {count} {verb} review ({today.astimezone(DIGEST_TIMEZONE):%b %-d})"
    admin_url = f"{origin}/admin/surveys/responses?status=needs_review" if origin else ""

    shown = sorted(rows, key=lambda r: r.created_at, reverse=True)[:DIGEST_ITEM_CAP]
    hidden = count - len(shown)
    headline = f"{count} {verb} review · {_stats_line(stats)}"

    text_lines = [headline, ""]
    html_items: list[str] = []
    for row in shown:
        rating = _rating_label(row)
        context = _context_line(row, school_names)
        comment = _comment_preview(row.comment)
        when = _when(row.created_at)
        page = _absolute_page_url(row.page_url, origin)

        text_lines.append(f"- {rating} · {context} · {when}")
        if comment:
            text_lines.append(f"  \"{comment}\"")
        if page:
            text_lines.append(f"  {page}")

        page_html = (
            f" · <a href='{escape(page, quote=True)}' style='color:#2563eb'>View page</a>" if page else ""
        )
        comment_html = (
            f"<div style='margin-top:4px;color:#111827'>&ldquo;{escape(comment)}&rdquo;</div>" if comment else ""
        )
        html_items.append(
            "<tr><td style='padding:10px 0;border-bottom:1px solid #e5e7eb'>"
            f"<div style='font-size:13px;color:#6b7280'><strong style='color:#111827'>{escape(rating)}</strong>"
            f" · {escape(context)} · {escape(when)}{page_html}</div>"
            f"{comment_html}</td></tr>"
        )

    if hidden > 0:
        text_lines.append(f"+{hidden} more")
        html_items.append(
            f"<tr><td style='padding:10px 0;color:#6b7280;font-size:13px'>+{hidden} more</td></tr>"
        )

    if admin_url:
        text_lines += ["", f"Review them: {admin_url}"]
        cta_html = (
            f"<p style='margin:20px 0'><a href='{escape(admin_url, quote=True)}' "
            "style='background:#111827;color:#ffffff;padding:10px 16px;border-radius:6px;"
            "text-decoration:none;font-weight:600'>Open Needs review</a></p>"
        )
    else:
        cta_html = ""

    html = (
        "<div style='font-family:sans-serif;font-size:14px;max-width:640px'>"
        f"<p style='font-size:15px'><strong>{escape(headline)}</strong></p>"
        "<p style='color:#6b7280'>Comments and negative ratings from the feedback widget "
        "that have not been in a digest yet. Mark them reviewed in the admin so the team "
        "knows they are handled.</p>"
        f"<table style='border-collapse:collapse;width:100%'>{''.join(html_items)}</table>"
        f"{cta_html}</div>"
    )
    return subject, html, "\n".join(text_lines)
