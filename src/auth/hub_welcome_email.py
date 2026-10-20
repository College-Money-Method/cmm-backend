"""Welcome email for a hub user added by a Super Admin or a director.

Sent from ``POST /api/v1/contacts`` once the login exists. Users provisioned by
the Airtable sync do not get it. The copy is a placeholder until Paul sends the
final version: swap the strings in ``_build`` and nothing else changes.

Only the *default* password (email handle + the school's resource-center
password) is put in the email. A custom password typed by a Super Admin is
never emailed; the email tells the user to ask whoever added them, or reset it.

A failed send is logged and swallowed: the account is already created, and the
admin's request must not fail because one email bounced.
"""

from __future__ import annotations

import logging
import uuid
from html import escape

from sqlalchemy.orm import Session

from src.emails.school_links import email_origin
from src.emails.ses_client import send_email
from src.paul_booking import email_layout as ui
from src.schools.models import Contact, School

logger = logging.getLogger(__name__)
SOURCE = "hub_welcome"
SUBJECT = "Your College Money Method Counselor Hub login"
PAUL_EMAIL = "paul.martin@collegemoneymethod.com"
FALLBACK_ADDER = "The College Money Method team"

# The text after "as " in the opening line, per hub role.
ROLE_LINES = {
    "hub_admin": ("an Admin", "Admins can also add other counselors to your school."),
    "hub_user": ("a User", ""),
    "viewer": ("a Viewer", ""),
}


def added_by_name(db: Session, user_id: uuid.UUID, email: str | None) -> str:
    """Display name of the admin who added the user: contact name, else email."""
    contact = db.query(Contact).filter(Contact.user_id == user_id).first()
    name = f"{contact.first_name or ''} {contact.last_name or ''}".strip() if contact else ""
    return name or email or FALLBACK_ADDER


def _build(*, first_name: str, email: str, default_password: str | None, role: str,
           school_name: str, added_by: str, login_url: str, reset_url: str) -> tuple[str, str]:
    """Return (html, text) for the welcome email."""
    role_label, role_note = ROLE_LINES.get(role, ROLE_LINES["hub_user"])
    password_line = (
        f"Password: {default_password}" if default_password
        else f"Password: the one {added_by} set for you. Ask them for it, or set your own at {reset_url}."
    )
    google_line = "You can sign in with your email and password, or with Google if your school uses Google Workspace."
    reset_line = f"Want your own password? Go to {reset_url}, enter your email, and we'll send you a link to set a new one."
    help_line = f"Questions? Contact Paul Martin at {PAUL_EMAIL} or your school's Counselor Hub admin."
    intro = f"{added_by} added you to the College Money Method Counselor Hub for {school_name} as {role_label}."
    if role_note:
        intro = f"{intro} {role_note}"

    login_items = [f"Login page: {login_url}", f"Email: {email}", password_line]
    html = ui.shell(
        "Welcome to the Counselor Hub",
        ui.para(f"Hi {first_name},")
        + ui.para(intro)
        + "<p><strong>Your login</strong><br>" + "<br>".join(escape(i) for i in login_items) + "</p>"
        + ui.button(login_url, "Log in to the Counselor Hub")
        + ui.para(google_line) + ui.para(reset_line) + ui.para(help_line),
    )
    text = "\n\n".join([
        f"Hi {first_name},", intro, "Your login\n" + "\n".join(login_items),
        google_line, reset_line, help_line, "The College Money Method team",
    ])
    return html, text


def send_hub_welcome_email(db: Session, *, to: str, first_name: str, default_password: str | None,
                           role: str, school: School, added_by: str) -> None:
    """Send the welcome email. Never raises; never logs the password."""
    origin = email_origin()
    if not origin:
        # email_origin() already logged the misconfiguration; a link-less login email is useless.
        logger.error("hub_welcome email skipped for %s: no email origin configured", to)
        return
    html, text = _build(
        first_name=first_name, email=to, default_password=default_password, role=role,
        school_name=school.name, added_by=added_by,
        login_url=f"{origin}/hub/login", reset_url=f"{origin}/forgot-password",
    )
    try:
        send_email(db, to, SUBJECT, html, text, SOURCE, school_id=school.id)
    except Exception:
        # A failure inside send_email's own DB writes leaves the session unusable;
        # roll back so the caller can still build its response. The new user's
        # rows were committed before this was called, so nothing is lost.
        db.rollback()
        logger.exception("hub_welcome email failed for %s", to)
