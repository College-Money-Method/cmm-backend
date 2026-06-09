"""FastAPI auth dependencies — JWT verification and role enforcement."""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from src.auth.models import UserRole
from src.auth.schemas import CurrentUser
from src.db.client import get_supabase
from src.db.deps import get_db

_bearer = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    db: Session = Depends(get_db),
    supabase=Depends(get_supabase),
) -> CurrentUser:
    """Verify JWT and return the current user with their app role."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        user_response = supabase.auth.get_user(token)
        if user_response is None or user_response.user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
        supabase_user = user_response.user
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    user_id = uuid.UUID(supabase_user.id)
    role_record = db.query(UserRole).filter(UserRole.user_id == user_id).first()

    if role_record is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No role assigned. Contact your administrator.",
        )

    return CurrentUser(
        user_id=user_id,
        role=role_record.role,
        school_id=role_record.school_id,
    )


def require_admin(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    if user.role != "super_admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def require_admin_or_viewer(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """Allow super_admin and viewer; counselors redirected to their own school endpoint."""
    if user.role not in ("super_admin", "viewer"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient permissions",
        )
    return user


def require_counselor(
    user: Annotated[CurrentUser, Depends(get_current_user)],
) -> CurrentUser:
    """Allow counselor hub users: super_admin, counselor, and viewer roles."""
    if user.role not in ("super_admin", "counselor", "viewer"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Counselor hub access required",
        )
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]
AdminDep = Annotated[CurrentUser, Depends(require_admin)]
AdminOrViewerDep = Annotated[CurrentUser, Depends(require_admin_or_viewer)]
CounselorDep = Annotated[CurrentUser, Depends(require_counselor)]
