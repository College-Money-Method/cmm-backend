"""Public (no-login) CAN-SPAM unsubscribe endpoint.

Reachable directly from an email footer link (see `unsubscribe.build_unsubscribe_url`)
— intentionally has no auth dependency (`AdminDep`/`CurrentUserDep`), since the person
clicking it is, by definition, not signed into the Hub. Authenticity instead comes from
the signed, expiring token; an invalid/forged/expired token is rejected with a generic
error that never reveals whether it would have matched a real contact.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import HTMLResponse
from sqlalchemy.exc import IntegrityError

from src.db.deps import DbDep
from src.emails.models import EmailSuppression
from src.emails.unsubscribe import verify_unsubscribe_token
from src.schools.models import Contact

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/emails", tags=["email-unsubscribe"])

_INVALID_TOKEN_DETAIL = "This unsubscribe link is invalid or has expired."

_CONFIRMATION_HTML = (
    "<!doctype html><html><head><meta charset=\"utf-8\">"
    "<title>Unsubscribed</title></head><body>"
    "<p>You've been unsubscribed from College Money Method automated emails.</p>"
    "</body></html>"
)


@router.get("/unsubscribe", response_class=HTMLResponse)
def unsubscribe(token: str, db: DbDep) -> HTMLResponse:
    """Flip `Contact.auto_emails` off and record an `EmailSuppression` row.

    Always returns the same generic confirmation page on a valid token, whether or
    not a matching (non-deleted) contact still exists — the response body must
    never leak contact details or existence.
    """
    contact_id = verify_unsubscribe_token(token)
    if contact_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=_INVALID_TOKEN_DETAIL)

    contact = db.query(Contact).filter(Contact.id == contact_id).first()
    if contact is not None:
        contact.auto_emails = False
        if contact.email:
            # Query-first (not an upsert): keeps this portable across the
            # Postgres prod DB and the SQLite DB used in tests, and a race here
            # is low-stakes (single-click, low-concurrency flow) unlike the
            # bounce/complaint webhook's on_conflict_do_nothing.
            existing = (
                db.query(EmailSuppression).filter(EmailSuppression.email == contact.email).first()
            )
            if existing is None:
                db.add(EmailSuppression(email=contact.email, reason="unsubscribe"))
        try:
            db.commit()
        except IntegrityError:
            # Concurrent unsubscribe click already inserted the suppression row —
            # the outcome (suppressed) is the same either way.
            db.rollback()
        logger.info("Unsubscribed contact %s via token link", contact_id)

    return HTMLResponse(content=_CONFIRMATION_HTML, status_code=status.HTTP_200_OK)
