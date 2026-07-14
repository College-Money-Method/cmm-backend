"""Auth and contact management endpoints."""

import logging
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status

logger = logging.getLogger(__name__)
from sqlalchemy import func, or_
from sqlalchemy.orm import Session, contains_eager, joinedload

from src.auth.deps import AdminDep, CurrentUserDep, get_current_user
from src.auth.hub_password import default_hub_password
from src.auth.models import Profile, UserRole
from src.auth.profile_sync import delete_profile, upsert_profile
from src.auth.schemas import (
    ContactCreate,
    ContactListResponse,
    ContactOut,
    ContactSyncResult,
    ContactUpdate,
    UserRoleOut,
)
from src.db.client import get_supabase
from src.db.deps import DbDep
from src.schools.models import Contact, School

router = APIRouter(tags=["auth"])


@router.get("/api/v1/auth/me", response_model=UserRoleOut)
def get_me(user: CurrentUserDep) -> UserRoleOut:
    """Return the current user's role and school assignment."""
    return UserRoleOut(
        user_id=user.user_id,
        role=user.role,
        school_id=user.school_id,
        school_role=user.school_role,
    )


# ──────────────────────────────────────────────
# Contact management (admin only)
# ──────────────────────────────────────────────


# Airtable-style display label derived from the access role.
_SCHOOL_ROLE_BY_ROLE = {"hub_admin": "Director", "hub_user": "Counselor"}


def _contact_out_from_role(
    role_record: UserRole, email: str, first: str, last: str
) -> ContactOut:
    first = first or ""
    last = last or ""
    full = f"{first} {last}".strip() or None
    school_name = role_record.school.name if role_record.school else None
    return ContactOut(
        user_id=role_record.user_id,
        email=email or "",
        first_name=first or None,
        last_name=last or None,
        full_name=full,
        role=role_record.role,
        school_id=role_record.school_id,
        school_name=school_name,
        title=role_record.title or None,
        school_role=role_record.school_role or None,
    )


def _contact_out(
    contact: Contact, role_record: UserRole | None, school: School | None
) -> ContactOut:
    """Build a contact row from a contact (+ its optional login role/school).

    Contacts without a school have no provisioned login yet, so user_id/role are
    None; the row still appears (e.g. under the "No School" filter) for assignment.
    """
    return ContactOut(
        id=contact.id,
        user_id=contact.user_id,
        email=contact.email,
        first_name=contact.first_name,
        last_name=contact.last_name,
        full_name=contact.full_name,
        role=role_record.role if role_record else None,
        school_id=contact.school_id,
        school_name=school.name if school else None,
        title=role_record.title if role_record else None,
        school_role=contact.role,
    )


def _build_contact_out(role_record: UserRole, auth_user: dict) -> ContactOut:
    """Build from a Supabase auth response dict (single-user endpoints)."""
    meta = auth_user.get("user_metadata", {})
    return _contact_out_from_role(
        role_record,
        auth_user.get("email", ""),
        meta.get("first_name") or "",
        meta.get("last_name") or "",
    )


def _sync_profile_from_auth(db: Session, user_id, auth_user: dict) -> None:
    """Mirror a Supabase auth response into the local profiles table."""
    meta = auth_user.get("user_metadata", {})
    upsert_profile(
        db,
        user_id,
        auth_user.get("email", ""),
        meta.get("first_name") or None,
        meta.get("last_name") or None,
    )


@router.post("/api/v1/contacts/sync-airtable", response_model=ContactSyncResult)
def sync_contacts_airtable(_admin: AdminDep, db: DbDep, supabase=Depends(get_supabase)) -> ContactSyncResult:
    """Provision missing counselor accounts from Airtable contacts."""
    from src.schools.sync import sync_counselors_from_airtable
    result = sync_counselors_from_airtable(db, supabase)
    return ContactSyncResult(**result)


@router.get("/api/v1/contacts", response_model=ContactListResponse)
def list_contacts(
    user: CurrentUserDep,
    db: DbDep,
    search: str | None = Query(default=None),
    school_id: uuid.UUID | None = Query(default=None),
    no_school: bool = Query(default=False),
    school_role: str | None = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
) -> ContactListResponse:
    """List contacts (Airtable is source of truth), left-joined to their login
    role + school. Includes contacts without a school (no login yet) so admins
    can find + assign them ("No School" filter).
    Super admins see all; counselors/viewers are scoped to their own school.
    """
    # Counselors and viewers may only query their own school
    if user.role not in ("super_admin",):
        if school_id is None:
            school_id = user.school_id
        elif school_id != user.school_id:
            raise HTTPException(status_code=403, detail="Access restricted to your own school")

    q = (
        db.query(Contact, UserRole, School)
        .outerjoin(UserRole, UserRole.user_id == Contact.user_id)
        .outerjoin(School, School.id == Contact.school_id)
        .filter(Contact.deleted_at.is_(None))
    )

    if no_school and user.role == "super_admin":
        q = q.filter(Contact.school_id.is_(None))
    elif school_id:
        q = q.filter(Contact.school_id == school_id)
    if school_role:
        q = q.filter(Contact.role == school_role)

    if search:
        like = f"%{search}%"
        q = q.filter(
            or_(
                Contact.email.ilike(like),
                Contact.first_name.ilike(like),
                Contact.last_name.ilike(like),
                Contact.full_name.ilike(like),
                School.name.ilike(like),
            )
        )

    total = q.count()
    rows = (
        q.order_by(Contact.full_name.nulls_last(), Contact.id)
        .offset(skip)
        .limit(limit)
        .all()
    )

    items = [_contact_out(c, ur, s) for c, ur, s in rows]
    return ContactListResponse(items=items, total=total, skip=skip, limit=limit)


@router.post("/api/v1/contacts", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
def create_contact(
    body: ContactCreate,
    current: CurrentUserDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> ContactOut:
    """Create a Supabase Auth user and assign them a counselor/director role.

    Super admins may create for any school with any role. Directors (hub_admin)
    may create counselors or directors for their own school only; the account
    password defaults to the email handle + the school's resource-center password.
    """
    # Authorization: super_admin (any school/role) or hub_admin (own school, hub roles)
    if current.role not in ("super_admin", "hub_admin"):
        raise HTTPException(status_code=403, detail="Hub admin access required")

    if current.role == "hub_admin":
        if not current.school_id:
            raise HTTPException(status_code=403, detail="You are not assigned to a school")
        # Directors create only for their own school and only counselor/director roles
        school_id = current.school_id
        if body.role not in ("hub_admin", "hub_user"):
            raise HTTPException(
                status_code=403, detail="Directors may only create counselors or directors"
            )
    else:
        school_id = body.school_id
        if not school_id:
            raise HTTPException(status_code=400, detail="school_id is required")

    # Verify school exists
    school = db.query(School).filter(School.id == school_id).first()
    if not school:
        raise HTTPException(status_code=404, detail="School not found")

    # Name defaults to the email handle so directors can supply just an email
    first_name = body.first_name or body.email.split("@", 1)[0]
    last_name = body.last_name or ""
    # Airtable-style display label follows the access role
    school_role = _SCHOOL_ROLE_BY_ROLE.get(body.role)

    # Create user in Supabase
    create_params = {
        "email": body.email,
        "user_metadata": {
            "first_name": first_name,
            "last_name": last_name,
        },
        "email_confirm": True,
    }
    # Explicit password wins; otherwise derive the default hub password (email handle
    # + the school's resource-center password, or just the handle when it has none).
    create_params["password"] = body.password or default_hub_password(
        body.email, school.cmm_website_password
    )

    logger.info("Creating Supabase user: email=%s school_id=%s role=%s", body.email, school_id, body.role)
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
        existing_role.school_id = school_id
        if school_role is not None:
            existing_role.school_role = school_role
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
        _sync_profile_from_auth(db, new_user.id, auth_user)
        db.commit()
        return _build_contact_out(role_record, auth_user)

    # Create role record
    role_record = UserRole(
        user_id=uuid.UUID(new_user.id),
        role=body.role,
        school_id=school_id,
        school_role=school_role,
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
    _sync_profile_from_auth(db, new_user.id, auth_user)
    db.commit()
    return _build_contact_out(role_record, auth_user)


@router.get("/api/v1/contacts/{user_id}", response_model=ContactOut)
def get_contact(
    user_id: uuid.UUID,
    _admin: AdminDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> ContactOut:
    """Fetch a single contact (counselor/viewer) by user_id."""
    role_record = (
        db.query(UserRole)
        .options(joinedload(UserRole.school))
        .filter(UserRole.user_id == user_id)
        .first()
    )
    if not role_record:
        raise HTTPException(status_code=404, detail="Contact not found")

    resp = supabase.auth.admin.get_user_by_id(str(user_id))
    if not resp or not resp.user:
        raise HTTPException(status_code=404, detail="Auth user not found")
    auth_user = {
        "email": resp.user.email or "",
        "user_metadata": resp.user.user_metadata or {},
    }
    return _build_contact_out(role_record, auth_user)


@router.patch("/api/v1/contacts/{user_id}", response_model=ContactOut)
def update_contact(
    user_id: uuid.UUID,
    body: ContactUpdate,
    user: CurrentUserDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> ContactOut:
    """Update a contact's profile. Super admins can update all fields.
    Counselors/viewers may only update the title of teammates at their own school.
    """
    role_record = (
        db.query(UserRole)
        .options(joinedload(UserRole.school))
        .filter(UserRole.user_id == user_id)
        .first()
    )
    if not role_record:
        raise HTTPException(status_code=404, detail="Contact not found")

    if user.role != "super_admin":
        # Scope: may only edit counselors at their own school
        if role_record.school_id != user.school_id:
            raise HTTPException(status_code=403, detail="Access restricted to your own school")
        # Field whitelist: only title is allowed
        update_data = {}
        if body.title is not None:
            update_data["title"] = body.title
    else:
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
    _sync_profile_from_auth(db, user_id, auth_user)
    db.commit()
    return _build_contact_out(role_record, auth_user)


@router.delete("/api/v1/contacts/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_contact(
    user_id: uuid.UUID,
    _admin: AdminDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> None:
    """Disable a contact's account (deletes Supabase user and role record)."""
    role_record = db.query(UserRole).filter(UserRole.user_id == user_id).first()
    if not role_record:
        raise HTTPException(status_code=404, detail="Contact not found")

    # Delete from Supabase
    try:
        supabase.auth.admin.delete_user(str(user_id))
    except Exception:
        pass

    db.delete(role_record)
    delete_profile(db, user_id)
    db.commit()
