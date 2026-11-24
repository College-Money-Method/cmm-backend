"""Resource Center session for a signed-in Counselor Hub user's own school.

Links the Hub opens into the school's Resource Center (assistant sources,
"Open in Resource Center" buttons) would otherwise hit the portal password gate
in every new tab. A hub login already proves more than the password does: the
Hub dashboard shows counselors that password. So the Hub trades its bearer for
the same session token the password check issues, for the user's own school
only (a super admin names the school they are working on).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException

from src.auth.deps import CurrentUserDep
from src.auth.hub_school import resolve_hub_school_id
from src.db.deps import DbDep
from src.schools.models import School
from src.schools.src_session import ensure_preview_active, issue_token

router = APIRouter(prefix="/api/v1/schools/me", tags=["schools"])


@router.post("/src-session")
def hub_src_session(user: CurrentUserDep, db: DbDep, school_id: uuid.UUID | None = None) -> dict:
    """``{slug, session_token, mode, preview_expires_at}`` for the caller's school."""
    school = db.get(School, resolve_hub_school_id(user, school_id))
    # Only schools whose Resource Center is open at all (customers, activated previews).
    if school is None or not (school.is_current_customer or school.is_cmm_website_activated):
        raise HTTPException(status_code=404, detail="src_not_available")
    ensure_preview_active(school)
    token, mode, preview_expires_at = issue_token(school)
    return {
        "slug": school.slug,
        "session_token": token,
        "mode": mode,
        "preview_expires_at": preview_expires_at.isoformat() if preview_expires_at else None,
    }
