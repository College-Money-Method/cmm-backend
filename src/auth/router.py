"""Auth and counselor management endpoints."""

import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

logger = logging.getLogger(__name__)
from sqlalchemy.orm import Session, joinedload

from src.auth.deps import AdminDep, CurrentUserDep, get_current_user
from src.auth.models import UserRole
from src.auth.schemas import (
    CounselorCreate,
    CounselorListResponse,
    CounselorOut,
    CounselorUpdate,
    UserRoleOut,
)
from src.db.client import get_supabase
from src.db.deps import DbDep
from src.schools.models import School

router = APIRouter(tags=["auth"])


@router.get("/api/v1/auth/me", response_model=UserRoleOut)
def get_me(user: CurrentUserDep) -> UserRoleOut:
    """Return the current user's role and school assignment."""
    return UserRoleOut(
        user_id=user.user_id,
        role=user.role,
        school_id=user.school_id,
    )


# ──────────────────────────────────────────────
# Counselor management (admin only)
# ──────────────────────────────────────────────


def _build_counselor_out(role_record: UserRole, auth_user: dict) -> CounselorOut:
    first = auth_user.get("user_metadata", {}).get("first_name") or ""
    last = auth_user.get("user_metadata", {}).get("last_name") or ""
    full = f"{first} {last}".strip() or None
    school_name = role_record.school.name if role_record.school else None
    return CounselorOut(
        user_id=role_record.user_id,
        email=auth_user.get("email", ""),
        first_name=first or None,
        last_name=last or None,
        full_name=full,
        role=role_record.role,
        school_id=role_record.school_id,
        school_name=school_name,
        title=role_record.title or None,
    )


@router.get("/api/v1/counselors", response_model=CounselorListResponse)
def list_counselors(
    _admin: AdminDep,
    db: DbDep,
    supabase=Depends(get_supabase),
    search: str | None = Query(default=None),
    school_id: uuid.UUID | None = Query(default=None),
    role: Literal["counselor", "viewer"] | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> CounselorListResponse:
    """List counselor/viewer accounts with pagination, search, and filters."""
    # Build base query
    q = (
        db.query(UserRole)
        .options(joinedload(UserRole.school))
        .filter(UserRole.role.in_(["counselor", "viewer"]))
    )

    if school_id:
        q = q.filter(UserRole.school_id == school_id)
    if role:
        q = q.filter(UserRole.role == role)

    role_records = q.order_by(UserRole.created_at).all()

    # Batch-fetch all Supabase auth users
    auth_users_map: dict[str, dict] = {}
    try:
        page = 1
        while True:
            users_resp = supabase.auth.admin.list_users(page=page, per_page=1000)
            users = users_resp if isinstance(users_resp, list) else []
            for u in users:
                auth_users_map[u.id] = {
                    "email": u.email or "",
                    "user_metadata": u.user_metadata or {},
                }
            if len(users) < 1000:
                break
            page += 1
    except Exception as exc:
        logger.warning("Could not batch-fetch Supabase users: %s", exc)

    # Build full list with auth data merged
    all_items: list[CounselorOut] = []
    for record in role_records:
        auth_user = auth_users_map.get(str(record.user_id))
        if not auth_user:
            continue
        all_items.append(_build_counselor_out(record, auth_user))

    # Apply search filter (on name, email, school_name)
    if search:
        term = search.lower()
        all_items = [
            c for c in all_items
            if term in (c.full_name or "").lower()
            or term in c.email.lower()
            or term in (c.school_name or "").lower()
        ]

    total = len(all_items)
    paginated = all_items[skip : skip + limit]

    return CounselorListResponse(items=paginated, total=total, skip=skip, limit=limit)


@router.post("/api/v1/counselors", response_model=CounselorOut, status_code=status.HTTP_201_CREATED)
def create_counselor(
    body: CounselorCreate,
    _admin: AdminDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> CounselorOut:
    """Create a Supabase Auth user and assign them a counselor/viewer role."""
    # Verify school exists
    school = db.query(School).filter(School.id == body.school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    # Create user in Supabase
    create_params = {
        "email": body.email,
        "user_metadata": {
            "first_name": body.first_name,
            "last_name": body.last_name,
        },
        "email_confirm": True,
    }
    if body.password:
        create_params["password"] = body.password

    logger.info("Creating Supabase user: email=%s school_id=%s role=%s", body.email, body.school_id, body.role)
    try:
        resp = supabase.auth.admin.create_user(create_params)
        if not resp or not resp.user:
            raise HTTPException(status_code=500, detail="Failed to create auth user")
        new_user = resp.user
        logger.info("Supabase user created: id=%s", new_user.id)
    except Exception as exc:
        logger.error("create_user failed: %s (type=%s)", exc, type(exc).__name__)
        # If the user already exists in Supabase Auth (e.g. from prior OAuth login),
        # find them by email and assign the role instead of failing.
        error_msg = str(exc).lower()
        if "already" in error_msg or "exists" in error_msg or "registered" in error_msg:
            logger.info("User exists in Supabase, looking up by email: %s", body.email)
            try:
                users_resp = supabase.auth.admin.list_users()
                existing = next(
                    (u for u in (users_resp or []) if u.email and u.email.lower() == body.email.lower()),
                    None,
                )
                logger.info("list_users found: %s", existing.id if existing else None)
            except Exception as list_exc:
                logger.error("list_users failed: %s", list_exc)
                existing = None
            if not existing:
                raise HTTPException(status_code=400, detail=str(exc))
            new_user = existing
        else:
            raise HTTPException(status_code=400, detail=str(exc))

    # Check if a role record already exists for this user
    existing_role = db.query(UserRole).filter(UserRole.user_id == uuid.UUID(new_user.id)).first()
    if existing_role:
        existing_role.role = body.role
        existing_role.school_id = body.school_id
        if body.title is not None:
            existing_role.title = body.title
        db.commit()
        db.refresh(existing_role)
        role_record = (
            db.query(UserRole)
            .options(joinedload(UserRole.school))
            .filter(UserRole.id == existing_role.id)
            .one()
        )
        auth_user = {
            "email": new_user.email or "",
            "user_metadata": getattr(new_user, "user_metadata", {}) or {},
        }
        return _build_counselor_out(role_record, auth_user)

    # Create role record
    role_record = UserRole(
        user_id=uuid.UUID(new_user.id),
        role=body.role,
        school_id=body.school_id,
        title=body.title,
    )
    db.add(role_record)
    db.commit()
    db.refresh(role_record)

    # Reload with school relationship
    role_record = (
        db.query(UserRole)
        .options(joinedload(UserRole.school))
        .filter(UserRole.id == role_record.id)
        .one()
    )

    auth_user = {
        "email": new_user.email or "",
        "user_metadata": new_user.user_metadata or {},
    }
    return _build_counselor_out(role_record, auth_user)


@router.get("/api/v1/counselors/{user_id}", response_model=CounselorOut)
def get_counselor(
    user_id: uuid.UUID,
    _admin: AdminDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> CounselorOut:
    """Fetch a single counselor/viewer by user_id."""
    role_record = (
        db.query(UserRole)
        .options(joinedload(UserRole.school))
        .filter(UserRole.user_id == user_id)
        .first()
    )
    if not role_record:
        raise HTTPException(status_code=404, detail="Counselor not found")

    resp = supabase.auth.admin.get_user_by_id(str(user_id))
    if not resp or not resp.user:
        raise HTTPException(status_code=404, detail="Auth user not found")
    auth_user = {
        "email": resp.user.email or "",
        "user_metadata": resp.user.user_metadata or {},
    }
    return _build_counselor_out(role_record, auth_user)


@router.patch("/api/v1/counselors/{user_id}", response_model=CounselorOut)
def update_counselor(
    user_id: uuid.UUID,
    body: CounselorUpdate,
    _admin: AdminDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> CounselorOut:
    """Update a counselor's school assignment or role."""
    role_record = (
        db.query(UserRole)
        .options(joinedload(UserRole.school))
        .filter(UserRole.user_id == user_id)
        .first()
    )
    if not role_record:
        raise HTTPException(status_code=404, detail="Counselor not found")

    # Use exclude_unset so explicitly-passed null (e.g. school_id=null) is honoured,
    # while omitted fields are ignored.
    update_data = body.model_dump(exclude_unset=True)
    if "school_id" in update_data and update_data["school_id"] is not None:
        school = db.query(School).filter(School.id == update_data["school_id"]).first()
        if not school:
            raise HTTPException(status_code=404, detail="School not found")

    for field, value in update_data.items():
        if hasattr(role_record, field):
            setattr(role_record, field, value)

    db.commit()
    db.refresh(role_record)

    # Update Supabase user metadata if name fields changed
    meta_update = {}
    if body.first_name is not None:
        meta_update["first_name"] = body.first_name
    if body.last_name is not None:
        meta_update["last_name"] = body.last_name
    if meta_update:
        try:
            supabase.auth.admin.update_user_by_id(str(user_id), {"user_metadata": meta_update})
        except Exception:
            pass

    resp = supabase.auth.admin.get_user_by_id(str(user_id))
    auth_user = {
        "email": resp.user.email or "" if resp and resp.user else "",
        "user_metadata": resp.user.user_metadata or {} if resp and resp.user else {},
    }

    role_record = (
        db.query(UserRole)
        .options(joinedload(UserRole.school))
        .filter(UserRole.user_id == user_id)
        .one()
    )
    return _build_counselor_out(role_record, auth_user)


@router.delete("/api/v1/counselors/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_counselor(
    user_id: uuid.UUID,
    _admin: AdminDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> None:
    """Disable a counselor account (deletes Supabase user and role record)."""
    role_record = db.query(UserRole).filter(UserRole.user_id == user_id).first()
    if not role_record:
        raise HTTPException(status_code=404, detail="Counselor not found")

    # Delete from Supabase
    try:
        supabase.auth.admin.delete_user(str(user_id))
    except Exception:
        pass

    db.delete(role_record)
    db.commit()
