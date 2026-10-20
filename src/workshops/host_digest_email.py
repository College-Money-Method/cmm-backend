"""Renders the day-before host digest: registrants' questions plus the live-page link.

Pure: no DB or network. Hosts span every school in a cohort session, so the email
carries no registrant identity — no names, emails or schools, and not the session
name (cohort names list the schools) — only each question, its grade and when it
was submitted. Workshop titles and free-text questions are untrusted (registrations
arrive through a public form), so every interpolated value is HTML-escaped and the
subject is stripped of CR/LF to rule out header injection.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from src.schools.display_timezone import resolve_display_timezone
from src.workshops.models import Webinar, WorkshopRegistration

COHOST_LOOKUP_BANNER = "We couldn't load this webinar's co-hosts from Zoom. Please forward this link."


def _one_line(value: str) -> str:
    """Collapse CR/LF (and runs of whitespace) so a value is safe in a header."""
    return " ".join(value.split())


def _multiline_html(value: str) -> str:
    """Escape first, then turn line breaks into <br> (so no markup can sneak in)."""
    return "<br>".join(escape(value).splitlines())


def _aware(value: datetime) -> datetime:
    # SQLite (tests) hands timestamps back naive; Postgres returns aware UTC.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def format_start(start: datetime, tz: ZoneInfo) -> str:
    """e.g. ``Mon, Oct 14, 6:00 PM EDT`` in the display timezone."""
    local = _aware(start).astimezone(tz)
    return f"{local:%a, %b} {local.day}, {local.hour % 12 or 12}:{local:%M %p %Z}"


def _workshop_title(webinar: Webinar) -> str:
    return webinar.workshop.name if webinar.workshop and webinar.workshop.name else "Workshop"


def _submitted_at(reg: WorkshopRegistration) -> datetime | None:
    # Zoom's registration time when we have it, else when the row arrived.
    raw = reg.registration_time or reg.created_at
    return _aware(raw) if raw else None


def _question_meta(reg: WorkshopRegistration, tz: ZoneInfo) -> str:
    """``Grade 11 · Submitted Oct 12`` — nothing that identifies the registrant."""
    parts = [f"Grade {reg.grade}"] if reg.grade else []
    submitted = _submitted_at(reg)
    if submitted:
        submitted = submitted.astimezone(tz)
        parts.append(f"Submitted {submitted:%b} {submitted.day}")
    return " · ".join(parts)


def render_host_digest(
    webinar: Webinar,
    registrations: Sequence[WorkshopRegistration],
    link: str,
    *,
    cohost_lookup_failed: bool = False,
    undelivered: Sequence[tuple[str, str]] = (),
    tz: ZoneInfo | None = None,
) -> tuple[str, str, str]:
    """Return (subject, html, text). ``registrations`` are the approved ones; questions
    are listed newest first, matching the live page.

    ``undelivered`` is (email, status) for co-hosts whose send this run did not
    reach "sent"; it is only passed to the extra recipients' copy.
    """
    tz = tz or resolve_display_timezone()
    label = _workshop_title(webinar)
    when = format_start(webinar.start_datetime, tz)
    subject = _one_line(f"Registrant questions: {label} · {when}")

    asked = sorted(
        (r for r in registrations if r.questions and r.questions.strip()),
        key=lambda r: _submitted_at(r) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    counts = f"{len(asked)} question(s) so far"
    footer = "The page updates as new questions come in."
    undelivered_line = (
        "Not delivered to: " + ", ".join(f"{email} ({status})" for email, status in undelivered)
        if undelivered
        else ""
    )

    # --- plain text ---
    text_lines = [label, when, "", counts, ""]
    if cohost_lookup_failed:
        text_lines += [COHOST_LOOKUP_BANNER, ""]
    text_lines += [f"Open live questions page: {link}", ""]
    if asked:
        for reg in asked:
            text_lines += [reg.questions.strip(), _question_meta(reg, tz), ""]
    else:
        text_lines += ["No questions yet.", ""]
    text_lines.append(footer)
    if undelivered_line:
        text_lines += ["", undelivered_line]

    # --- html ---
    banner = (
        f"<p style='background:#fff4e5;padding:8px 12px;border-radius:4px'>{escape(COHOST_LOOKUP_BANNER)}</p>"
        if cohost_lookup_failed
        else ""
    )
    if asked:
        items = "".join(
            f"<li style='margin-bottom:12px'>{_multiline_html(reg.questions.strip())}<br>"
            f"<span style='color:#667085'>{escape(_question_meta(reg, tz))}</span>"
            "</li>"
            for reg in asked
        )
        questions_html = f"<ul style='padding-left:18px'>{items}</ul>"
    else:
        questions_html = "<p><em>No questions yet.</em></p>"
    undelivered_html = f"<p style='color:#b42318'>{escape(undelivered_line)}</p>" if undelivered_line else ""
    html = (
        "<div style='font-family:Arial,sans-serif;font-size:14px;line-height:1.5'>"
        f"<h2 style='margin-bottom:0'>{escape(label)}</h2>"
        f"<p style='margin-top:4px'>{escape(when)}</p>"
        f"<p>{escape(counts)}</p>"
        f"{banner}"
        f"<p><a href='{escape(link, quote=True)}' style='display:inline-block;background:#1d4ed8;color:#fff;"
        "padding:10px 16px;border-radius:6px;text-decoration:none'>Open live questions page</a></p>"
        f"{questions_html}"
        f"<p style='color:#667085'>{escape(footer)}</p>"
        f"{undelivered_html}"
        "</div>"
    )
    return subject, html, "\n".join(text_lines)
