"""FastAPI router for guest contact submissions."""

import uuid

from fastapi import APIRouter, HTTPException, Query, Request, status

from src.auth.deps import AdminDep
from src.auth.rate_limit import allow, client_ip
from src.db.deps import DbDep
from src.guest_contacts.models import GuestContact
from src.guest_contacts.schemas import (
    GuestContactCreate,
    GuestContactDetail,
    GuestContactReceipt,
)
from src.guest_contacts.spam_detection import ADMIN_MARKED, ADMIN_RESTORED, detect_spam

router = APIRouter(prefix="/api/v1/guest-contacts", tags=["guest-contacts"])

# A family sending a follow-up minutes after their first note is normal; a dozen
# posts from one address in ten minutes is not.
_SUBMIT_LIMIT = 3
_SUBMIT_WINDOW_SECONDS = 600.0


# ── Public endpoint (no auth) ───────────────────────────────────────

@router.post("", response_model=GuestContactReceipt, status_code=status.HTTP_201_CREATED)
def submit_guest_contact(body: GuestContactCreate, request: Request, db: DbDep):
    """Public endpoint — guests submit contact info from the website.

    Open to the internet with no CAPTCHA, so it carries two guards. A per-IP rate
    limit blunts flooding (in-memory and per-process, so a speed bump rather than
    a guarantee), and ``detect_spam`` quarantines what looks automated. A
    quarantined row is still stored and still answers 201: the submitter is told
    nothing, and an admin can rescue a false positive from the Spam tab.
    """
    if not allow(
        f"guest-contact:{client_ip(request)}",
        limit=_SUBMIT_LIMIT,
        window_seconds=_SUBMIT_WINDOW_SECONDS,
    ):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many submissions. Please wait a few minutes and try again.",
        )

    spam_reason = detect_spam(
        first_name=body.first_name,
        last_name=body.last_name,
        email=body.email,
        school_name=body.school_name,
        message=body.message,
        honeypot=body.website,
    )

    gc = GuestContact(
        **body.model_dump(exclude_none=True),
        is_spam=spam_reason is not None,
        spam_reason=spam_reason,
    )
    db.add(gc)
    db.commit()
    db.refresh(gc)
    return GuestContactReceipt.model_validate(gc)


# ── Admin endpoints ──────────────────────────────────────────────────

@router.get("", response_model=list[GuestContactDetail])
def list_guest_contacts(
    db: DbDep,
    _admin: AdminDep,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    spam: bool = Query(default=False, description="Return quarantined submissions instead."),
):
    """List guest contact submissions (admin only), newest first.

    Defaults to the clean inbox; ``spam=true`` returns the quarantine.
    """
    rows = (
        db.query(GuestContact)
        .filter(GuestContact.is_spam.is_(spam))
        .order_by(GuestContact.created_at.desc())
        .offset(skip)
        .limit(limit)
        .all()
    )
    return [GuestContactDetail.model_validate(r) for r in rows]


@router.get("/counts")
def guest_contact_counts(db: DbDep, _admin: AdminDep) -> dict[str, int]:
    """Inbox and spam totals, so the admin tabs can show how much is in each."""
    spam_total = db.query(GuestContact).filter(GuestContact.is_spam.is_(True)).count()
    total = db.query(GuestContact).count()
    return {"inbox": total - spam_total, "spam": spam_total}


@router.get("/{gc_id}", response_model=GuestContactDetail)
def get_guest_contact(gc_id: uuid.UUID, db: DbDep, _admin: AdminDep):
    """Get a single guest contact by ID (admin only)."""
    gc = db.query(GuestContact).filter(GuestContact.id == gc_id).first()
    if not gc:
        raise HTTPException(status_code=404, detail="Guest contact not found")
    return GuestContactDetail.model_validate(gc)


@router.patch("/{gc_id}/spam", response_model=GuestContactDetail)
def set_guest_contact_spam(
    gc_id: uuid.UUID,
    db: DbDep,
    _admin: AdminDep,
    is_spam: bool = Query(description="True to quarantine, false to restore to the inbox."),
):
    """Move a submission between the inbox and the quarantine (admin only).

    The heuristics will occasionally be wrong in both directions; this is how an
    admin overrides them. The reason is rewritten to say a human made the call —
    in both directions, so that a rescued row is distinguishable from an
    unexamined one and the backfill can leave it alone.
    """
    gc = db.query(GuestContact).filter(GuestContact.id == gc_id).first()
    if not gc:
        raise HTTPException(status_code=404, detail="Guest contact not found")
    gc.is_spam = is_spam
    gc.spam_reason = ADMIN_MARKED if is_spam else ADMIN_RESTORED
    db.commit()
    db.refresh(gc)
    return GuestContactDetail.model_validate(gc)


@router.delete("/{gc_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_guest_contact(gc_id: uuid.UUID, db: DbDep, _admin: AdminDep):
    """Delete a guest contact (admin only)."""
    gc = db.query(GuestContact).filter(GuestContact.id == gc_id).first()
    if not gc:
        raise HTTPException(status_code=404, detail="Guest contact not found")
    db.delete(gc)
    db.commit()
