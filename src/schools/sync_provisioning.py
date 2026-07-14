"""Provision Supabase auth users + UserRoles FROM the contacts table.

Contacts are the source of truth; auth accounts and roles are derived.
Never reads Airtable directly — run sync_contacts_from_airtable first.
"""
from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.hub_password import default_hub_password
from src.auth.models import UserRole
from src.auth.profile_sync import delete_profile, upsert_profile
from src.config import settings
from src.schools.models import Contact, School
from src.schools.sync_utils import should_revoke_access

logger = logging.getLogger(__name__)


def _reconcile_revocations(
    db: Session, all_contacts: list[Contact], roles: list[UserRole]
) -> int:
    """Soft-revoke counselor access whose driving contact is no longer active.

    Active = a contact with a school link, not deactivated, and a provisioned
    user_id. Only sync-managed, non-super_admin roles are revoked; the Supabase
    auth user is intentionally KEPT (reversible — re-linking restores it).

    Honors settings.sync_enable_revoke: when False, logs what WOULD be revoked
    without acting (first-deploy safety against surprise mass-offboarding).
    """
    active_ids = {
        str(c.user_id)
        for c in all_contacts
        if c.user_id and c.school_id is not None and c.deleted_at is None
    }
    managed_ids = {str(c.user_id) for c in all_contacts if c.user_id}

    # `revoked` counts revocation CANDIDATES so a log-only run still reports the
    # blast radius. Deletes only happen when sync_enable_revoke is on.
    revoked = 0
    for role in roles:
        uid = str(role.user_id)
        if not should_revoke_access(uid, role.role, active_ids, managed_ids):
            continue
        revoked += 1
        if settings.sync_enable_revoke:
            logger.info("Revoking counselor access for user_id=%s (role=%s)", uid, role.role)
            db.delete(role)
            delete_profile(db, role.user_id)
        else:
            logger.warning(
                "[log-only] WOULD revoke access for user_id=%s (role=%s) — "
                "set SYNC_ENABLE_REVOKE=true to act",
                uid, role.role,
            )
    return revoked


def _fetch_supabase_users_by_email(supabase: object) -> dict[str, object]:
    """Batch-fetch all Supabase auth users, keyed by lowercased email."""
    users_by_email: dict[str, object] = {}
    page = 1
    while True:
        users = supabase.auth.admin.list_users(page=page, per_page=1000)
        users = users if isinstance(users, list) else []
        for u in users:
            if u.email:
                users_by_email[u.email.lower()] = u
        if len(users) < 1000:
            return users_by_email
        page += 1


def provision_counselors_from_contacts(db: Session, supabase: object) -> dict:
    """
    For every contact with an email:
    - Ensure a Supabase auth user exists; write its id back to contact.user_id
    - Ensure a UserRole exists (Director → hub_admin, else hub_user)
    - Keep role/school_role in sync with contact.role (never touch super_admin)

    Returns {"counselors_created", "school_roles_updated", "skipped"}
    """
    # Access rule: only school-linked, non-deactivated contacts get provisioned.
    contacts: list[Contact] = (
        db.execute(
            select(Contact).where(
                Contact.email.isnot(None),
                Contact.school_id.isnot(None),
                Contact.deleted_at.is_(None),
            )
        )
        .scalars()
        .all()
    )

    all_roles: list[UserRole] = db.execute(select(UserRole)).scalars().all()
    role_by_user_id: dict[str, UserRole] = {str(r.user_id): r for r in all_roles}

    # School resource-center passwords, for deriving default account passwords
    password_by_school_id: dict[uuid.UUID, str | None] = dict(
        db.execute(select(School.id, School.cmm_website_password)).all()
    )

    try:
        supabase_users_by_email = _fetch_supabase_users_by_email(supabase)
    except Exception as exc:
        logger.error("Failed to fetch Supabase users: %s", exc)
        raise

    # contacts.user_id is UNIQUE — a duplicate-email contact can't share a user.
    # Seed from ALL contacts (not just the provisioning target) so a user_id
    # already owned by an unlinked/deactivated contact is still protected;
    # otherwise re-assigning it here would hit uq_contacts_user_id.
    claimed_user_ids: set[str] = {
        str(uid)
        for (uid,) in db.execute(
            select(Contact.user_id).where(Contact.user_id.isnot(None))
        ).all()
    }

    counselors_created = school_roles_updated = skipped = 0

    for contact in contacts:
        email = (contact.email or "").strip()
        if not email:
            skipped += 1
            continue
        email_lower = email.lower()

        school_role = contact.role or "Counselor"
        system_role = "hub_admin" if school_role == "Director" else "hub_user"

        # ── Resolve auth user (existing link → email match → create) ──
        user_id_str: str | None = str(contact.user_id) if contact.user_id else None
        if not user_id_str:
            auth_user = supabase_users_by_email.get(email_lower)
            if auth_user is None:
                try:
                    create_params = {
                        "email": email,
                        "user_metadata": {
                            "first_name": contact.first_name or "",
                            "last_name": contact.last_name or "",
                        },
                        "email_confirm": True,
                    }
                    # Default password: email handle + the school's resource-center
                    # password (just the handle when the school has none) — never an invite
                    create_params["password"] = default_hub_password(
                        email, password_by_school_id.get(contact.school_id)
                    )
                    resp = supabase.auth.admin.create_user(create_params)
                    if not resp or not resp.user:
                        logger.error("create_user returned no user for %s", email)
                        skipped += 1
                        continue
                    auth_user = resp.user
                    supabase_users_by_email[email_lower] = auth_user
                except Exception as exc:
                    logger.error("create_user failed for %s: %s", email, exc)
                    skipped += 1
                    continue
            user_id_str = auth_user.id

            if user_id_str in claimed_user_ids:
                # Another contact row already owns this auth user (duplicate
                # email in data). Skip entirely — do NOT sync the shared role,
                # or this duplicate would silently overwrite the first contact's
                # school_role/school context.
                logger.warning(
                    "Auth user %s already linked to another contact — skipping duplicate email %s",
                    user_id_str, email,
                )
                skipped += 1
                continue
            contact.user_id = uuid.UUID(user_id_str)
            claimed_user_ids.add(user_id_str)

        # Keep the local profiles mirror in sync (contact is the name source).
        if user_id_str:
            upsert_profile(db, user_id_str, email, contact.first_name, contact.last_name)

        # ── Upsert UserRole (contact.role is source of truth) ──
        existing_role = role_by_user_id.get(user_id_str)
        if existing_role:
            changed = False
            if existing_role.school_role != school_role:
                existing_role.school_role = school_role
                changed = True
            if existing_role.role != "super_admin" and existing_role.role != system_role:
                logger.info("Updated role for %s: %s → %s", email, existing_role.role, system_role)
                existing_role.role = system_role
                changed = True
            if changed:
                school_roles_updated += 1
            continue

        try:
            # SAVEPOINT: isolate a per-record failure so it can't roll back the
            # whole batch (db.rollback() would undo every prior flush this run).
            with db.begin_nested():
                user_role = UserRole(
                    user_id=uuid.UUID(user_id_str),
                    role=system_role,
                    school_id=contact.school_id,
                    school_role=school_role,
                )
                db.add(user_role)
                db.flush()
            role_by_user_id[user_id_str] = user_role
            counselors_created += 1
            logger.info("Created counselor role: email=%s role=%s", email, system_role)
        except Exception as exc:
            logger.error("Failed to create UserRole for %s: %s", email, exc)
            skipped += 1

    # Reconcile: revoke access for contacts that lost their school link or were
    # deactivated (Airtable-driven offboarding). Re-fetch contacts AND roles from
    # the DB so the reconcile never operates on stale/expunged in-memory objects.
    all_contacts: list[Contact] = db.execute(select(Contact)).scalars().all()
    all_roles: list[UserRole] = db.execute(select(UserRole)).scalars().all()
    counselors_revoked = _reconcile_revocations(db, all_contacts, all_roles)

    db.commit()
    logger.info(
        "Counselor provisioning complete: created=%d roles_updated=%d revoked=%d skipped=%d",
        counselors_created, school_roles_updated, counselors_revoked, skipped,
    )
    return {
        "counselors_created": counselors_created,
        "school_roles_updated": school_roles_updated,
        "counselors_revoked": counselors_revoked,
        "skipped": skipped,
    }
