"""The five booking emails. Each goes through ``send_email`` (suppression and
sandbox apply) with source ``paul_booking``. A failed send is logged and
swallowed: the request or decision it follows is already committed, and one
bounced address must not hide the other recipient's email."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from html import escape

from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from src.emails.ses_client import send_email
from src.paul_booking import email_layout as ui
from src.paul_booking.ics import ICS_MIME, build_ics
from src.paul_booking.models import PaulMeetingRequest
from src.paul_booking.review_token import make_review_token
from src.paul_booking.rules import as_aware

if TYPE_CHECKING:
    from src.paul_booking.service import BookingSettings

logger = logging.getLogger(__name__)
SOURCE = "paul_booking"


def _send(db: Session, to: str, subject: str, html: str, text: str, attachments=None) -> None:
    subject = " ".join(subject.split())  # a header must be one line, whatever the visitor typed
    try:
        send_email(db, to, subject, html, text, SOURCE, attachments=attachments)
    except Exception:
        logger.error("paul_booking email failed (subject=%r)", subject, exc_info=True)


def _name(req: PaulMeetingRequest) -> str:
    return f"{req.contact.first_name} {req.contact.last_name or ''}".strip()


def notify_new_request(db: Session, req: PaulMeetingRequest, cfg: BookingSettings) -> None:
    """Visitor 'request received' (with the deadline) and Paul's review email."""
    c = req.contact
    visitor_tz = ui.zone_or_utc(req.visitor_timezone)
    deadline = ui.when_text(req.expires_at, visitor_tz)
    other_time = ui.site_link("/book-a-call")
    slot = ui.when_text(req.start_at, visitor_tz, req.duration_minutes)
    _send(
        db, c.email, "We received your request for a call with Paul",
        ui.shell("Request received", ui.para(f"Hi {c.first_name}, thanks for asking for a call with Paul Martin.")
                 + ui.para(f"Requested time: {slot}.")
                 + ui.para(f"Paul will confirm by {deadline}. If he can't, the time is released and you are welcome to pick another.")
                 + ui.button(other_time, "Pick another time")),
        f"Hi {c.first_name}, we received your request for {slot}.\nPaul will confirm by {deadline}. "
        f"If he can't, the time is released.\nPick another time: {other_time}",
    )

    review = ui.site_link(f"/booking-requests/{make_review_token(req.id, req.expires_at)}")
    calendar = ui.site_link("/admin/workshops/calendar")
    paul_slot = ui.when_text(req.start_at, cfg.tz, req.duration_minutes)
    paul_deadline = ui.when_text(req.expires_at, cfg.tz)
    rows = [("Who", f"{_name(req)} <{c.email}>"), ("School", c.school_name or "-"), ("Role", c.role or "-"),
            ("When", paul_slot), ("Reply by", paul_deadline), ("Message", c.message or "-")]
    if c.is_parent:
        rows.append(("Note", "Looks like a parent, not a counselor"))
    table = "".join(f"<tr><td style='padding:2px 12px 2px 0;color:#6b7280'>{k}</td><td>{escape(v)}</td></tr>" for k, v in rows)
    _send(
        db, cfg.notify_email, f"Call request: {_name(req)} ({c.school_name or 'no school'})",
        ui.shell("New call request", f"<table>{table}</table>" + ui.button(review, "Review request")
                 + f'<p>Or open the <a href="{escape(calendar, quote=True)}">admin calendar</a>.</p>'),
        "\n".join(f"{k}: {v}" for k, v in rows) + f"\nReview: {review}\nAdmin calendar: {calendar}",
    )


def notify_approved(db: Session, req: PaulMeetingRequest, cfg: BookingSettings, now: datetime) -> None:
    """Join link + .ics to the visitor and to Paul."""
    c = req.contact
    start = as_aware(req.start_at)
    end = start + timedelta(minutes=req.duration_minutes)
    join_url = req.zoom_join_url or ""
    ics = [("call-with-paul.ics", ICS_MIME, build_ics(
        uid=str(req.id), start=start, duration_minutes=req.duration_minutes,
        summary=f"College Money Method x {c.school_name or _name(req)}",
        description=f"Zoom: {join_url}", location=join_url,
        organizer_email=cfg.notify_email, now=now))]

    visitor_slot = ui.when_text(start, ui.zone_or_utc(req.visitor_timezone), req.duration_minutes)
    gcal = ui.google_calendar_url(title="Call with Paul Martin", start=start, end=end,
                                  details=f"Zoom: {join_url}", location=join_url)
    _send(
        db, c.email, "Your call with Paul is confirmed",
        ui.shell("You're confirmed", ui.para(f"Hi {c.first_name}, Paul confirmed your call: {visitor_slot}.")
                 + ui.button(join_url, "Join on Zoom")
                 + f'<p><a href="{escape(gcal, quote=True)}">Add to Google Calendar</a> (or open the attached .ics).</p>'),
        f"Hi {c.first_name}, Paul confirmed your call: {visitor_slot}.\nJoin: {join_url}\nAdd to calendar: {gcal}",
        ics,
    )
    paul_slot = ui.when_text(start, cfg.tz, req.duration_minutes)
    _send(
        db, cfg.notify_email, f"Confirmed: {_name(req)} - {paul_slot}",
        ui.shell("Call confirmed", ui.para(f"{_name(req)} ({c.school_name or 'no school'}): {paul_slot}.")
                 + ui.button(join_url, "Zoom link")),
        f"{_name(req)} ({c.school_name or 'no school'}): {paul_slot}\nZoom: {join_url}",
        ics,
    )


def notify_declined(db: Session, req: PaulMeetingRequest) -> None:
    c = req.contact
    slot = ui.when_text(req.start_at, ui.zone_or_utc(req.visitor_timezone), req.duration_minutes)
    other_time = ui.site_link("/book-a-call")
    note = (req.decline_note or "").strip()
    _send(
        db, c.email, "About your call request with Paul",
        ui.shell("We couldn't make that time", ui.para(f"Hi {c.first_name}, unfortunately Paul can't take the call on {slot}.")
                 + (ui.para(f"Paul's note: {note}") if note else "") + ui.button(other_time, "Pick another time")),
        f"Hi {c.first_name}, Paul can't take the call on {slot}."
        + (f"\nPaul's note: {note}" if note else "") + f"\nPick another time: {other_time}",
    )
