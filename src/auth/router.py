"""Auth and contact management endpoints."""

import logging
import uuid
from datetime import datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

logger = logging.getLogger(__name__)
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, contains_eager, joinedload

from src.auth.deps import AdminDep, CurrentUserDep, get_current_user
from src.auth.hub_password import default_hub_password
from src.auth.models import Profile, UserRole
from src.auth.profile_sync import delete_profile, upsert_profile
from src.auth.rate_limit import allow
from src.auth.schemas import (
    AuthEmailSyncOut,
    ChangePasswordRequest,
    CurrentUser,
    CheckEmailOut,
    CheckEmailRequest,
    ContactCreate,
    ContactListResponse,
    ContactOut,
    ContactSyncResult,
    ContactUpdate,
    HubPasswordResetOut,
    HubPasswordResetRequest,
    MePreferencesUpdate,
    UserRoleOut,
)
from supabase import create_client

from src.config import settings
from src.db.client import get_supabase
from src.db.deps import DbDep
from src.emails.email_preferences import sync_unsubscribe_suppression
from src.schools.models import Contact, School

router = APIRouter(tags=["auth"])


@router.get("/api/v1/auth/me", response_model=UserRoleOut)
def get_me(user: CurrentUserDep, db: DbDep) -> UserRoleOut:
    """Return the current user's role, school assignment, and hub preferences.

    The preferences are read from the Contact row rather than the role record: a
    super_admin has no Contact and simply gets None for all of them, which the
    Hub reads as "use the browser's zone" and "no opt-ins to show".
    """
    prefs = db.execute(
        select(Contact.timezone, Contact.auto_emails, Contact.broadcast_emails).where(
            Contact.user_id == user.user_id
        )
    ).first()
    return UserRoleOut(
        user_id=user.user_id,
        role=user.role,
        school_id=user.school_id,
        school_role=user.school_role,
        timezone=prefs.timezone if prefs else None,
        auto_emails=prefs.auto_emails if prefs else None,
        broadcast_emails=prefs.broadcast_emails if prefs else None,
    )


@router.patch("/api/v1/auth/me/preferences", response_model=UserRoleOut)
def update_my_preferences(
    body: MePreferencesUpdate, user: CurrentUserDep, db: DbDep
) -> UserRoleOut:
    """Update the signed-in user's own hub preferences.

    Scoped to the caller's own Contact row by `user_id`, so it needs no role
    check: there is no request shape that reaches somebody else's settings.
    """
    contact = db.query(Contact).filter(Contact.user_id == user.user_id).first()
    if contact is None:
        # No Contact row (a super_admin, typically). Nothing to store, and
        # nothing is broken — the Hub falls back to the browser's zone.
        raise HTTPException(status_code=404, detail="No contact record for this account")

    fields = body.model_dump(exclude_unset=True)
    if "timezone" in fields:
        contact.timezone = fields["timezone"]
    if fields.get("auto_emails") is not None:
        contact.auto_emails = fields["auto_emails"]
    if fields.get("broadcast_emails") is not None:
        contact.broadcast_emails = fields["broadcast_emails"]
    if fields.get("auto_emails") is not None or fields.get("broadcast_emails") is not None:
        # An `EmailSuppression` row blocks every send whatever the opt-ins say,
        # so opting back in here has to lift an earlier unsubscribe — otherwise
        # the contact keeps receiving nothing while the Hub shows them opted in.
        sync_unsubscribe_suppression(db, contact)
    db.commit()
    db.refresh(contact)

    return UserRoleOut(
        user_id=user.user_id,
        role=user.role,
        school_id=user.school_id,
        school_role=user.school_role,
        timezone=contact.timezone,
        auto_emails=contact.auto_emails,
        broadcast_emails=contact.broadcast_emails,
    )


@router.post("/api/v1/auth/check-email", response_model=CheckEmailOut)
def check_email(body: CheckEmailRequest, request: Request, db: DbDep) -> CheckEmailOut:
    """Public: report whether an email belongs to a registered user.

    The forgot-password form calls this before Supabase so it can warn on an
    unknown/typo'd address — Supabase, by design, returns 200 and stays silent
    for unknown emails, which otherwise leaves the user staring at a reset link
    that never arrives. This intentionally exposes account existence (an accepted
    product trade-off), so it is rate-limited per client IP to blunt enumeration
    scraping. Existence is read from `profiles`, the indexed mirror of
    `auth.users`.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    client_ip = fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")
    if not allow(f"check-email:{client_ip}", limit=5, window_seconds=60.0):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts. Please wait a minute and try again.",
        )

    exists = (
        db.query(Profile.user_id)
        .filter(func.lower(Profile.email) == body.email.lower())
        .first()
        is not None
    )
    return CheckEmailOut(exists=exists)


@router.post("/api/v1/auth/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    user: CurrentUserDep,
    supabase=Depends(get_supabase),
) -> None:
    """Change the signed-in user's own hub password.

    Verifies the current password server-side (via a password sign-in), then updates
    it with the Supabase admin API. Using the admin API sidesteps the browser
    `updateUser` path, which can be silently blocked by the project's "Secure
    password change" (reauthentication-nonce) setting.
    """
    # Resolve the caller's email from their auth record.
    resp = supabase.auth.admin.get_user_by_id(str(user.user_id))
    email = resp.user.email if resp and resp.user else None
    if not email:
        raise HTTPException(status_code=404, detail="Auth user not found")

    # Verify the current password with a throwaway anon client so we never mutate
    # the admin client's session state.
    anon_key = settings.supabase_key or settings.supabase_service_role_key
    verifier = create_client(settings.supabase_url, anon_key)
    try:
        signin = verifier.auth.sign_in_with_password(
            {"email": email, "password": body.current_password}
        )
        if not signin or not signin.user:
            raise ValueError("no session")
    except Exception:
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    finally:
        try:
            verifier.auth.sign_out()
        except Exception:
            pass

    try:
        supabase.auth.admin.update_user_by_id(
            str(user.user_id), {"password": body.new_password}
        )
    except Exception as exc:
        logger.error("Failed to change password for %s: %s", email, exc)
        raise HTTPException(status_code=502, detail="Failed to update password")


# ──────────────────────────────────────────────
# Contact management (admin only)
# ──────────────────────────────────────────────


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
        # No Contact row available here (see docstring) — a brand-new contact
        # can't have opted in yet, so default False. Subsequent GETs go through
        # `_contact_out`, which reads the real values.
        auto_emails=False,
        broadcast_emails=False,
    )


def _contact_out(
    contact: Contact,
    role_record: UserRole | None,
    school: School | None,
    auth_email: str | None = None,
) -> ContactOut:
    """Build a contact row from a contact (+ its optional login role/school).

    Contacts without a school have no provisioned login yet, so user_id/role are
    None; the row still appears (e.g. under the "No School" filter) for assignment.
    `auth_email` is only passed by the detail endpoint (see ContactOut).
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
        auth_email=auth_email,
        auto_emails=contact.auto_emails,
        broadcast_emails=contact.broadcast_emails,
        is_airtable_managed=bool(contact.airtable_id),
    )


def _fetch_auth_email(supabase, user_id) -> str | None:
    """Return the Supabase auth user's current email, or None if unavailable.

    Never raises: a Supabase hiccup must not break the contact detail page — the
    caller just loses the mismatch check for that render.
    """
    try:
        resp = supabase.auth.admin.get_user_by_id(str(user_id))
        return resp.user.email if resp and resp.user else None
    except Exception as exc:
        logger.warning("Could not fetch auth email for user_id=%s: %s", user_id, exc)
        return None


def _emails_match(a: str | None, b: str | None) -> bool:
    """Case-insensitive email comparison (Supabase lowercases; Airtable may not)."""
    return (a or "").strip().lower() == (b or "").strip().lower()


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


def _upsert_contact_row(
    db: Session,
    user_id: uuid.UUID,
    email: str,
    first_name: str,
    last_name: str,
    school_id: uuid.UUID | None,
    school_role: str | None,
) -> None:
    """Keep the contacts table (source of the school contact lists) coherent with
    a provisioned login: adding a contact with a role = granting hub access.
    Match by user_id first, then by unclaimed email; create the row when missing.
    """
    contact = db.query(Contact).filter(Contact.user_id == user_id).first()
    if contact is None and email:
        contact = (
            db.query(Contact)
            .filter(func.lower(Contact.email) == email.lower(), Contact.user_id.is_(None))
            .first()
        )
    if contact is None:
        contact = Contact(email=email or None)
        db.add(contact)
    contact.user_id = user_id
    contact.school_id = school_id
    if first_name:
        contact.first_name = first_name
    if last_name:
        contact.last_name = last_name
    # Airtable-style display label (Director/Counselor); keep an existing label
    if school_role and not contact.role:
        contact.role = school_role
    contact.deleted_at = None


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
    role: str | None = Query(default=None),
    # Email compose only: hides prospect-school contacts, who are not addressable
    # (see emails.audience — a prospect never receives CMM mail).
    customer_schools_only: bool = Query(default=False),
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
    if customer_schools_only:
        q = q.filter(School.is_current_customer.is_(True))
    if school_role:
        q = q.filter(Contact.role == school_role)
    # Hub permission filter: the access role on the login. "no_access" = contacts
    # with no provisioned login (user_id is null).
    if role:
        if role == "no_access":
            q = q.filter(Contact.user_id.is_(None))
        else:
            q = q.filter(UserRole.role == role)

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

    # "No access": create a contact record only — no Supabase login, no UserRole.
    # School is optional here (a login-less contact needn't belong to a school).
    if body.role == "no_access":
        no_access_school_id = current.school_id if current.role == "hub_admin" else body.school_id
        if no_access_school_id is not None:
            if not db.query(School).filter(School.id == no_access_school_id).first():
                raise HTTPException(status_code=404, detail="School not found")
        existing = (
            db.query(Contact)
            .filter(func.lower(Contact.email) == body.email.lower(), Contact.deleted_at.is_(None))
            .first()
        )
        if existing is not None and existing.user_id is not None:
            raise HTTPException(status_code=409, detail=f"{body.email} already has hub access.")
        contact = existing or Contact(email=body.email)
        if existing is None:
            db.add(contact)
        if body.first_name:
            contact.first_name = body.first_name
        if body.last_name:
            contact.last_name = body.last_name
        contact.school_id = no_access_school_id
        contact.deleted_at = None
        db.commit()
        db.refresh(contact)
        school = (
            db.query(School).filter(School.id == no_access_school_id).first()
            if no_access_school_id
            else None
        )
        return _contact_out(contact, None, school)

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
    # Director/Counselor label is decoupled from hub permission — it is sourced
    # from Airtable on sync, never derived from the access role. Leave it unset
    # here; the next Airtable sync populates it (only when still empty).
    school_role = None

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
    # Track whether we created the auth user fresh (password already applied) vs.
    # reused a pre-existing one (e.g. from a prior OAuth login) that still has no
    # usable password — the latter must have the password set explicitly below.
    user_pre_existed = False
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
            user_pre_existed = True
        else:
            raise HTTPException(status_code=400, detail=str(exc))

    # Check if a role record already exists for this user
    existing_role = db.query(UserRole).filter(UserRole.user_id == uuid.UUID(new_user.id)).first()
    if existing_role and existing_role.role == "super_admin":
        # Don't demote a platform admin into a school counselor/director.
        raise HTTPException(
            status_code=409,
            detail=f"{body.email} is an admin account and can't be added as a counselor.",
        )

    # A pre-existing auth user (created via OAuth, or previously without a password)
    # would have no usable email/password credential — set it so the counselor can
    # log in with the email + default/explicit password. Skipped for freshly created
    # users, whose password was already applied at create time.
    if user_pre_existed:
        try:
            supabase.auth.admin.update_user_by_id(
                new_user.id, {"password": create_params["password"]}
            )
            logger.info("Set password on pre-existing auth user: id=%s", new_user.id)
        except Exception as exc:
            logger.error("Failed to set password on existing user %s: %s", body.email, exc)
            raise HTTPException(status_code=502, detail="Failed to set account password")
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
        _upsert_contact_row(
            db, uuid.UUID(new_user.id), body.email, first_name, last_name, school_id, school_role
        )
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
    _upsert_contact_row(
        db, uuid.UUID(new_user.id), body.email, first_name, last_name, school_id, school_role
    )
    db.commit()
    return _build_contact_out(role_record, auth_user)


@router.get("/api/v1/contacts/{contact_id}", response_model=ContactOut)
def get_contact(
    contact_id: uuid.UUID,
    _admin: AdminDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> ContactOut:
    """Fetch a single contact by its contact id (works with or without a login).

    Keyed on the contacts table so login-less contacts (no provisioned auth user)
    are editable too — the admin detail page uses this for every row.

    Also resolves the auth user's own email so the UI can flag the case where an
    Airtable rename left the login on the old address (see ContactOut.auth_email).
    """
    row = (
        db.query(Contact, UserRole, School)
        .outerjoin(UserRole, UserRole.user_id == Contact.user_id)
        .outerjoin(School, School.id == Contact.school_id)
        .filter(Contact.id == contact_id, Contact.deleted_at.is_(None))
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Contact not found")
    contact, role_record, school = row
    auth_email = _fetch_auth_email(supabase, contact.user_id) if contact.user_id else None
    return _contact_out(contact, role_record, school, auth_email=auth_email)


@router.patch("/api/v1/contacts/{contact_id}", response_model=ContactOut)
def update_contact(
    contact_id: uuid.UUID,
    body: ContactUpdate,
    user: CurrentUserDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> ContactOut:
    """Update a contact by its contact id.

    Super admins update any field (name, school — including clearing it to null — and,
    when a login exists, role/title). Directors (hub_admin) and counselors/viewers may
    only manage provisioned teammates at their own school (directors: name/title/role
    limited to counselor/director; others: title only). Login-less contacts are
    super-admin only.
    """
    contact = (
        db.query(Contact)
        .filter(Contact.id == contact_id, Contact.deleted_at.is_(None))
        .first()
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")

    role_record = None
    if contact.user_id is not None:
        role_record = (
            db.query(UserRole)
            .options(joinedload(UserRole.school))
            .filter(UserRole.user_id == contact.user_id)
            .first()
        )

    if user.role == "super_admin":
        # exclude_unset so an explicit null (e.g. school_id=null) clears the field,
        # while omitted fields are left untouched.
        update_data = body.model_dump(exclude_unset=True)
    else:
        # Non-admins manage only provisioned teammates at their own school.
        if role_record is None:
            raise HTTPException(status_code=403, detail="Hub admin access required")
        if role_record.school_id != user.school_id:
            raise HTTPException(status_code=403, detail="Access restricted to your own school")
        # Defense in depth: a non-admin (hub_user/viewer) can never change any
        # role — including their own — regardless of what the frontend shows.
        # The UI hides the role Select for self-edit, but that alone is not a
        # security boundary.
        if user.role != "hub_admin" and body.role is not None:
            raise HTTPException(
                status_code=403, detail="Only hub admins may change hub permissions"
            )
        update_data = {}
        if user.role == "hub_admin":
            # Directors: name, title, and access role (limited to counselor/director).
            if body.first_name is not None:
                update_data["first_name"] = body.first_name
            if body.last_name is not None:
                update_data["last_name"] = body.last_name
            if body.title is not None:
                update_data["title"] = body.title
            if body.role is not None:
                if body.role not in ("hub_admin", "hub_user"):
                    raise HTTPException(
                        status_code=403,
                        detail="Directors may only assign counselor or director roles",
                    )
                update_data["role"] = body.role
        else:
            # Counselors/viewers may only update the title of teammates.
            if body.title is not None:
                update_data["title"] = body.title
        # Self-edit: any hub user may toggle their OWN email opt-ins, regardless
        # of role. Additive to the role-based branches above and scoped strictly
        # to these two fields — checked against `user.user_id` (self), not the
        # broader same-school check used elsewhere in this branch, so it can
        # never be used to edit another teammate's fields.
        if contact.user_id is not None and contact.user_id == user.user_id:
            if body.auto_emails is not None:
                update_data["auto_emails"] = body.auto_emails
            if body.broadcast_emails is not None:
                update_data["broadcast_emails"] = body.broadcast_emails

    if "school_id" in update_data and update_data["school_id"] is not None:
        school = db.query(School).filter(School.id == update_data["school_id"]).first()
        if not school:
            raise HTTPException(status_code=404, detail="School not found")

    # Contacts row: name + school assignment (source of the school contact lists).
    if "first_name" in update_data:
        contact.first_name = update_data["first_name"]
    if "last_name" in update_data:
        contact.last_name = update_data["last_name"]
    if "school_id" in update_data:
        contact.school_id = update_data["school_id"]
    if "auto_emails" in update_data:
        contact.auto_emails = update_data["auto_emails"]
    if "broadcast_emails" in update_data:
        contact.broadcast_emails = update_data["broadcast_emails"]
    if "auto_emails" in update_data or "broadcast_emails" in update_data:
        # An `EmailSuppression` row blocks every send whatever the opt-ins say,
        # so opting back in here has to lift an earlier unsubscribe — otherwise
        # the contact keeps receiving nothing while the Hub shows them opted in.
        sync_unsubscribe_suppression(db, contact)

    # Login-backed fields (role/title) live on the UserRole; mirror name to Supabase
    # + the profiles table. Hub permission (role) is decoupled from the school_role label.
    if role_record is not None:
        if update_data.get("role") is not None:
            role_record.role = update_data["role"]
        if "title" in update_data:
            role_record.title = update_data["title"]
        if "school_id" in update_data:
            role_record.school_id = update_data["school_id"]
        meta_update = {}
        if "first_name" in update_data:
            meta_update["first_name"] = update_data["first_name"]
        if "last_name" in update_data:
            meta_update["last_name"] = update_data["last_name"]
        if meta_update:
            try:
                supabase.auth.admin.update_user_by_id(
                    str(contact.user_id), {"user_metadata": meta_update}
                )
            except Exception:
                pass
            _sync_profile_from_auth(
                db,
                contact.user_id,
                {
                    "email": contact.email or "",
                    "user_metadata": {
                        "first_name": contact.first_name or "",
                        "last_name": contact.last_name or "",
                    },
                },
            )

    db.commit()
    db.refresh(contact)
    if role_record is not None:
        db.refresh(role_record)

    school = (
        db.query(School).filter(School.id == contact.school_id).first()
        if contact.school_id
        else None
    )
    return _contact_out(contact, role_record, school)


def _revoke_hub_login(db: Session, supabase, contact: Contact) -> None:
    """Strip a contact's hub login: delete the Supabase user, role record and profile,
    then detach the login from the contact. Does not commit.

    The contacts row survives — the person remains a contact without access.
    """
    user_id = contact.user_id
    if user_id is None:
        return
    try:
        supabase.auth.admin.delete_user(str(user_id))
    except Exception:
        pass

    role_record = db.query(UserRole).filter(UserRole.user_id == user_id).first()
    contact.user_id = None
    if role_record is not None:
        db.delete(role_record)
    delete_profile(db, user_id)


@router.post(
    "/api/v1/contacts/{contact_id}/revoke-access", status_code=status.HTTP_204_NO_CONTENT
)
def revoke_contact_access(
    contact_id: uuid.UUID,
    _admin: AdminDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> None:
    """Revoke a contact's hub login but keep them as a contact.

    Deletes the Supabase user + role record; the contacts row stays so the person
    still receives email and appears in the school's contact list. To remove the
    person entirely, use DELETE (which cascades through this first).
    """
    contact = (
        db.query(Contact)
        .filter(Contact.id == contact_id, Contact.deleted_at.is_(None))
        .first()
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    if contact.user_id is None:
        raise HTTPException(status_code=400, detail="Contact has no hub login to revoke")

    _revoke_hub_login(db, supabase, contact)
    db.commit()


def _authorize_contact_delete(user: CurrentUser, contact: Contact) -> None:
    """Decide whether `user` may delete `contact`. Raises 400/403 otherwise.

    Super admins may delete any contact. Directors (hub_admin) may delete their own
    school's contacts, so they can offboard a teammate they added themselves without
    filing a request. Counselors and viewers may not delete anyone — mirrors
    `update_contact`, where managing somebody else is hub-admin only.

    Nobody may delete their own contact: the cascade destroys the caller's Supabase
    user, so it would revoke the session mid-request and lock a school's last
    director out of their own hub.
    """
    if contact.user_id is not None and contact.user_id == user.user_id:
        raise HTTPException(
            status_code=400, detail="You can't delete your own contact — ask another admin."
        )
    if user.role == "super_admin":
        return
    if user.role != "hub_admin":
        raise HTTPException(status_code=403, detail="Hub admin access required")
    # A contact with no school (never assigned) belongs to no director.
    if contact.school_id is None or contact.school_id != user.school_id:
        raise HTTPException(status_code=403, detail="Access restricted to your own school")


@router.delete("/api/v1/contacts/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_contact(
    contact_id: uuid.UUID,
    user: CurrentUserDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> None:
    """Soft-delete a contact: cascades through hub-login revocation, then sets
    ``deleted_at`` so the person drops out of every list and email audience.

    Open to super admins and to directors for their own school — see
    `_authorize_contact_delete`.

    Airtable-sourced contacts are rejected — Airtable owns those rows, and the next
    sync would reactivate them (see sync_contacts.py). Offboard them by removing the
    record in Airtable instead.
    """
    contact = (
        db.query(Contact)
        .filter(Contact.id == contact_id, Contact.deleted_at.is_(None))
        .first()
    )
    if not contact:
        raise HTTPException(status_code=404, detail="Contact not found")
    _authorize_contact_delete(user, contact)
    if contact.airtable_id:
        raise HTTPException(
            status_code=400,
            detail="This contact is managed in Airtable — remove the record there instead.",
        )

    # A contact with a login can't just be hidden: leaving the Supabase user alive
    # would keep a working password for a person who no longer exists here.
    _revoke_hub_login(db, supabase, contact)
    contact.deleted_at = datetime.now(timezone.utc)
    db.commit()


@router.post(
    "/api/v1/contacts/{contact_id}/reset-hub-password", response_model=HubPasswordResetOut
)
def reset_hub_password(
    contact_id: uuid.UUID,
    _admin: AdminDep,
    db: DbDep,
    body: HubPasswordResetRequest = HubPasswordResetRequest(),
    supabase=Depends(get_supabase),
) -> HubPasswordResetOut:
    """Reset a contact's hub login password.

    When `body.password` is provided it becomes the new password; otherwise it resets
    to the deterministic default (email handle + the school's resource-center password,
    just the handle when the school has none). Returns the new password so the admin
    can share it.
    """
    contact = (
        db.query(Contact)
        .filter(Contact.id == contact_id, Contact.deleted_at.is_(None))
        .first()
    )
    if not contact or contact.user_id is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    user_id = contact.user_id
    role_record = (
        db.query(UserRole)
        .options(joinedload(UserRole.school))
        .filter(UserRole.user_id == user_id)
        .first()
    )
    if not role_record:
        raise HTTPException(status_code=404, detail="Contact not found")

    custom = (body.password or "").strip()
    if custom:
        new_password = custom
    else:
        resp = supabase.auth.admin.get_user_by_id(str(user_id))
        email = resp.user.email if resp and resp.user else None
        if not email:
            raise HTTPException(status_code=404, detail="Auth user not found")
        rc_password = role_record.school.cmm_website_password if role_record.school else None
        new_password = default_hub_password(email, rc_password)

    try:
        supabase.auth.admin.update_user_by_id(str(user_id), {"password": new_password})
    except Exception as exc:
        logger.error("Failed to reset hub password for %s: %s", user_id, exc)
        raise HTTPException(status_code=502, detail="Failed to reset password")

    return HubPasswordResetOut(password=new_password)


@router.post(
    "/api/v1/contacts/{contact_id}/sync-auth-email", response_model=AuthEmailSyncOut
)
def sync_auth_email(
    contact_id: uuid.UUID,
    _admin: AdminDep,
    db: DbDep,
    supabase=Depends(get_supabase),
) -> AuthEmailSyncOut:
    """Point the contact's Supabase auth user at the contact's current email.

    Fixes the Airtable-rename case: `sync_contacts_from_airtable` updates
    contacts.email but nothing propagates to auth.users, so the person can only log
    in with their old address. Renaming the existing auth user (rather than creating
    a new one) preserves user_id, so the UserRole, school link and password all
    survive — and the old address stops working, which a new user wouldn't achieve.
    """
    contact = (
        db.query(Contact)
        .filter(Contact.id == contact_id, Contact.deleted_at.is_(None))
        .first()
    )
    if not contact or contact.user_id is None:
        raise HTTPException(status_code=404, detail="Contact not found")

    new_email = (contact.email or "").strip()
    if not new_email:
        raise HTTPException(status_code=400, detail="Contact has no email to sync")

    # Read the auth user directly (not _fetch_auth_email): here a Supabase failure
    # must abort rather than be swallowed, or we'd report a bogus result.
    try:
        resp = supabase.auth.admin.get_user_by_id(str(contact.user_id))
    except Exception as exc:
        logger.error("Failed to fetch auth user %s: %s", contact.user_id, exc)
        raise HTTPException(status_code=502, detail="Could not reach Supabase Auth")
    auth_user = resp.user if resp else None
    if not auth_user:
        raise HTTPException(status_code=404, detail="Auth user not found")

    previous_email = auth_user.email
    if _emails_match(previous_email, new_email):
        # Already in sync — no write, so the UI can just clear its warning.
        return AuthEmailSyncOut(
            updated=False, previous_email=previous_email, auth_email=previous_email or new_email
        )

    # Another contact already owning this email means duplicate data upstream;
    # renaming would create two logins for one address. Bail out with a clear reason.
    conflict = (
        db.query(Contact)
        .filter(
            func.lower(Contact.email) == new_email.lower(),
            Contact.id != contact.id,
            Contact.user_id.isnot(None),
            Contact.deleted_at.is_(None),
        )
        .first()
    )
    if conflict is not None:
        raise HTTPException(
            status_code=409,
            detail=f"{new_email} already belongs to another contact with hub access.",
        )

    try:
        # email_confirm keeps the account usable immediately — no confirmation email,
        # matching how provisioning creates these accounts in the first place.
        supabase.auth.admin.update_user_by_id(
            str(contact.user_id), {"email": new_email, "email_confirm": True}
        )
    except Exception as exc:
        logger.error(
            "Failed to sync auth email for %s (%s → %s): %s",
            contact.user_id, previous_email, new_email, exc,
        )
        # Most likely cause: the address is already registered to a different auth
        # user (one with no contact row, so the check above missed it).
        raise HTTPException(
            status_code=409,
            detail=f"Could not set the login email to {new_email}. It may already be registered.",
        )

    # Keep the local mirror consistent with what we just wrote.
    upsert_profile(db, contact.user_id, new_email, contact.first_name, contact.last_name)
    db.commit()
    logger.info(
        "Synced auth email for contact %s: %s → %s", contact.id, previous_email, new_email
    )
    return AuthEmailSyncOut(updated=True, previous_email=previous_email, auth_email=new_email)
