"""Host-page access token and link lifetime for a webinar.

The token is random and stored on the row (not an HMAC) so rotating it revokes
the old link. The link lives until a week after the session effectively ends.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from src.emails.school_links import email_origin
from src.db.enums import RegistrationStatus
from src.workshops.models import Webinar, WorkshopRegistration

# Used when a webinar has a start but no end, for both link TTL and digest selection.
DEFAULT_SESSION_LENGTH = timedelta(hours=2)
HOST_LINK_TTL_AFTER_END = timedelta(days=7)


def _new_token() -> str:
    return secrets.token_urlsafe(32)  # 43 url-safe chars


def ensure_host_access_token(db: Session, webinar: Webinar) -> str:
    """The webinar's token, minting one if absent. Never overwrites an existing token.

    The UPDATE only matches while the column is NULL, so two concurrent callers
    cannot each mint a different token: the loser matches no row and re-reads.
    """
    claimed = db.execute(
        update(Webinar)
        .where(Webinar.id == webinar.id, Webinar.host_access_token.is_(None))
        .values(host_access_token=_new_token())
        .returning(Webinar.host_access_token)
    ).scalar_one_or_none()
    db.commit()
    if claimed is not None:
        db.refresh(webinar)
        return claimed
    token = db.execute(select(Webinar.host_access_token).where(Webinar.id == webinar.id)).scalar_one_or_none()
    if token is None:
        raise LookupError(f"Webinar {webinar.id} no longer exists")
    db.refresh(webinar)
    return token


def rotate_host_access_token(db: Session, webinar: Webinar) -> str:
    """Replace the token unconditionally; the previous link stops working at once."""
    token = _new_token()
    db.execute(update(Webinar).where(Webinar.id == webinar.id).values(host_access_token=token))
    db.commit()
    db.refresh(webinar)
    return token


def effective_end(webinar: Webinar) -> datetime | None:
    """End time, else start + 2h, else None (no start means no valid window)."""
    if webinar.end_datetime is not None:
        return webinar.end_datetime
    if webinar.start_datetime is not None:
        return webinar.start_datetime + DEFAULT_SESSION_LENGTH
    return None


def host_link_expires_at(webinar: Webinar) -> datetime | None:
    """When the host link stops working; None means the link is not valid."""
    end = effective_end(webinar)
    return end + HOST_LINK_TTL_AFTER_END if end is not None else None


def host_questions_url(token: str) -> str:
    """Absolute page link. Raises RuntimeError when no public origin is configured,
    since a relative link is useless in an email or a copied message."""
    origin = email_origin()
    if not origin:
        raise RuntimeError("email_origin() is empty; cannot build the host questions link")
    return f"{origin}/workshop-hosts/{token}"


def approved_registrations(db: Session, webinar_id: uuid.UUID) -> list[WorkshopRegistration]:
    """Approved registrants oldest first, school preloaded: what hosts may see."""
    return list(
        db.execute(
            select(WorkshopRegistration)
            .where(
                WorkshopRegistration.webinar_id == webinar_id,
                WorkshopRegistration.status == RegistrationStatus.APPROVED,
            )
            .options(selectinload(WorkshopRegistration.school))
            .order_by(WorkshopRegistration.created_at)
        )
        .scalars()
        .all()
    )
