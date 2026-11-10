"""Which school's restricted content a family chat may draw from.

The school id comes from the client, so it is honoured only for someone who can
prove they belong to that school: a staff login for it, or a valid
resource-center session token. A missing token is never tolerated here, even
while ``src_session_enforced`` is off: one shared password must not let a caller
enumerate every school's restricted chunks.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from src.assistant.retrieval_scope import Audience, RetrievalScope
from src.auth.schemas import CurrentUser
from src.schools.models import School
from src.schools.src_session import Mode, authorize, is_staff_for


def scope_for_school(school: School, audience: Audience, mode: Mode) -> RetrievalScope:
    """The scope a school's member sees; a preview school gets the public one."""
    if mode == "preview":
        return RetrievalScope.public(audience)
    return RetrievalScope(audience, school.id, school.cohort_id, school.state)


def family_school_scope(
    db, school_id: uuid.UUID | None, src_token: str | None, user: CurrentUser | None
) -> RetrievalScope:
    """Resolve the family retrieval scope for the requested school (None = public)."""
    if school_id is None:
        return RetrievalScope.public("family")
    school = db.get(School, school_id)
    if school is None:
        raise HTTPException(status_code=404, detail="assistant_school_not_found")
    if is_staff_for(user, school.id):
        return scope_for_school(school, "family", "full")
    if not src_token:
        raise HTTPException(status_code=403, detail="assistant_school_forbidden")
    # Validates the token (bad -> 401/403, expired preview -> 403) and reports its mode.
    session = authorize(school, src_token, user)
    return scope_for_school(school, "family", session.mode)
