"""Which school a hub login operates on.

Counselors always use the school on their own role; the school id a client
sends is honoured only for a super_admin, who has no school of their own.
"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from src.auth.schemas import CurrentUser


def resolve_hub_school_id(user: CurrentUser, requested: uuid.UUID | None) -> uuid.UUID:
    if user.role == "super_admin":
        if requested is None:
            raise HTTPException(status_code=422, detail="school_required")
        return requested
    if user.school_id is None:
        raise HTTPException(status_code=403, detail="No school associated with this account")
    return user.school_id
