"""Public (no-login) email-preference API backing the frontend preference page.

Reachable from an email footer link, so it intentionally has no auth dependency
(`AdminDep`/`CurrentUserDep`) — the person clicking is, by definition, not
signed into the Hub. Authenticity comes from the signed, expiring token instead;
an invalid/forged/expired token is rejected with a generic error that never
reveals whether it would have matched a real contact.

The page itself lives in the frontend (`/email-preferences`), so the API origin
never appears in an email. ``GET /unsubscribe`` stays only to redirect links
from already-sent emails there.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from src.db.deps import DbDep
from src.emails.email_preferences import sync_unsubscribe_suppression
from src.emails.unsubscribe import email_preferences_url, verify_unsubscribe_token_ids
from src.schools.models import Contact

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/emails", tags=["email-preferences"])

_INVALID_TOKEN_DETAIL = "This unsubscribe link is invalid or has expired."


class EmailPreferencesOut(BaseModel):
    """Current opt-in state behind a token. Carries no contact details: the link
    is holder-authenticated, and a grouped school send hands the same link to
    several counselors, so naming an address would disclose one recipient's data
    to another."""

    auto_emails: bool
    broadcast_emails: bool


class EmailPreferencesUpdate(BaseModel):
    token: str = Field(min_length=1)
    auto_emails: bool
    broadcast_emails: bool


def _contacts_for(token: str, db: DbDep) -> list[Contact]:
    """Contacts encoded in `token`, or a 400 when it doesn't verify.

    A token may cover several contacts: a grouped school send addresses one
    email to every counselor at a school, and they all see the same footer link,
    so it has to cover all of them or whoever clicks could only change a
    colleague's preferences. Whatever is chosen therefore applies to every
    recipient of that one email.
    """
    contact_ids = verify_unsubscribe_token_ids(token)
    if not contact_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_TOKEN_DETAIL)
    return db.query(Contact).filter(Contact.id.in_(contact_ids)).all()


@router.get("/preferences", response_model=EmailPreferencesOut)
def get_preferences(token: str, db: DbDep) -> EmailPreferencesOut:
    """Opt-in state for `token`'s contact(s) — read-only, so a mail client or
    link scanner prefetching the footer link can never unsubscribe anyone.

    A valid token whose contact no longer exists still answers 200 (both off):
    the response must not reveal whether a contact exists.
    """
    contacts = _contacts_for(token, db)
    # On a grouped token the boxes reflect "anyone still opted in", so a
    # colleague's earlier opt-out doesn't silently propose unsubscribing the rest.
    return EmailPreferencesOut(
        auto_emails=any(c.auto_emails for c in contacts),
        broadcast_emails=any(c.broadcast_emails for c in contacts),
    )


@router.put("/preferences", response_model=EmailPreferencesOut)
def save_preferences(payload: EmailPreferencesUpdate, db: DbDep) -> EmailPreferencesOut:
    """Apply the chosen opt-ins to every contact the token covers.

    Both false is the unsubscribe case — including a mail client's RFC 8058
    one-click, which the frontend route forwards here since that gesture carries
    no finer intent.
    """
    contacts = _contacts_for(payload.token, db)

    for contact in contacts:
        contact.auto_emails = payload.auto_emails
        contact.broadcast_emails = payload.broadcast_emails
        # Suppression blocks every send regardless of the opt-ins, so it has to
        # follow them in both directions — including lifting an earlier
        # unsubscribe when someone opts back in here.
        sync_unsubscribe_suppression(db, contact)

    if contacts:
        try:
            db.commit()
        except IntegrityError:
            # Concurrent click already inserted the suppression row — the
            # outcome (suppressed) is the same either way.
            db.rollback()
        logger.info(
            "Email preferences updated for %d contact(s) via token link (auto=%s, broadcast=%s)",
            len(contacts),
            payload.auto_emails,
            payload.broadcast_emails,
        )

    return EmailPreferencesOut(
        auto_emails=payload.auto_emails, broadcast_emails=payload.broadcast_emails
    )


@router.get("/unsubscribe")
def unsubscribe_redirect(token: str) -> RedirectResponse:
    """Send an already-sent email's link to the frontend preference page.

    Kept because unsubscribe tokens live a year: emails posted before the page
    moved must keep working. The token is passed through unvalidated — the page
    (and this API) reject a bad one, and validating here would only tell a
    guesser whether their token was real.
    """
    return RedirectResponse(
        url=email_preferences_url(token), status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )
