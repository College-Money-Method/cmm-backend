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
from src.auth.profile_sync import upsert_profile
from src.schools.models import Contact, School

logger = logging.getLogger(__name__)


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
    contacts: list[Contact] = (
        db.execute(select(Contact).where(Contact.email.isnot(None))).scalars().all()
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

    # contacts.user_id is UNIQUE — duplicate-email contacts can't share a user
    claimed_user_ids: set[str] = {str(c.user_id) for c in contacts if c.user_id}

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
                # email in data) — leave user_id NULL, still sync the role below
                logger.warning(
                    "Auth user %s already linked to another contact — duplicate email %s",
                    user_id_str, email,
                )
            else:
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
            db.rollback()
            skipped += 1

    db.commit()
    logger.info(
        "Counselor provisioning complete: created=%d roles_updated=%d skipped=%d",
        counselors_created, school_roles_updated, skipped,
    )
    return {
        "counselors_created": counselors_created,
        "school_roles_updated": school_roles_updated,
        "skipped": skipped,
    }
