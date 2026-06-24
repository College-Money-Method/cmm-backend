"""Airtable → DB sync logic for schools, contacts, and counselor auth accounts."""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.auth.models import UserRole
from src.cycles.models import Cohort
from src.integrations.airtable import get_cohorts_records, get_contacts_records, get_schools_records
from src.schools.models import Contact, School
from src.schools.slug_utils import unique_slug

logger = logging.getLogger(__name__)


def _parse_bool(value: object) -> bool:
    """Normalize Airtable checkbox fields (True/False/None/"true"/"false")."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value) if value is not None else False


def _parse_int(value: object) -> int | None:
    """Safely cast enrollment-style fields to int."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def sync_schools_contacts_from_airtable(db: Session, supabase: object) -> dict:
    """
    Pull Airtable Schools + Contacts + Cohorts and sync into DB.

    Rules:
    - Upsert schools: create new, update is_current_customer + cohort_id + airtable_id on existing
    - For ALL schools (new and existing), sync contacts from Airtable
    - Dedup contacts by airtable_id first, then email+school_id
    - For contacts with email → create Supabase auth user + UserRole(counselor)
    - Handle per-record errors without aborting entire sync

    Returns {"schools_created", "schools_updated", "contacts_created", "counselors_created", "skipped", "synced_at"}
    """
    # ── 1. Fetch all Airtable records ────────────────────────────────────────
    at_schools = get_schools_records()
    at_contacts = get_contacts_records()
    at_cohorts = get_cohorts_records()

    # ── 2. Build cohort lookup maps (Airtable rec ID → DB Cohort) ────────────
    all_cohorts: list[Cohort] = db.execute(select(Cohort)).scalars().all()
    cohort_by_airtable_id: dict[str, Cohort] = {c.airtable_id: c for c in all_cohorts if c.airtable_id}
    cohort_by_name: dict[str, Cohort] = {c.name: c for c in all_cohorts}

    # Also index incoming Airtable cohort records by their rec ID → name for fallback
    at_cohort_name_by_id: dict[str, str] = {
        rec["id"]: (rec.get("fields", {}).get("Name") or "")
        for rec in at_cohorts
    }

    def _resolve_cohort(cohort_rec_ids: list[str]) -> Cohort | None:
        """Return the first DB cohort that matches any of the given Airtable rec IDs."""
        for cid in cohort_rec_ids:
            cohort = cohort_by_airtable_id.get(cid)
            if cohort:
                return cohort
            # Fallback: match by name from Airtable cohort table
            name = at_cohort_name_by_id.get(cid)
            if name:
                cohort = cohort_by_name.get(name)
                if cohort:
                    return cohort
        return None

    # ── 3. Build school lookup maps (existing DB schools) ────────────────────
    all_schools: list[School] = db.execute(select(School)).scalars().all()
    school_by_airtable_id: dict[str, School] = {s.airtable_id: s for s in all_schools if s.airtable_id}
    # Slug is unique in DB — most reliable dedup key after airtable_id
    school_by_slug: dict[str, School] = {s.slug: s for s in all_schools if s.slug}
    school_by_name: dict[str, School] = {s.name.strip().lower(): s for s in all_schools if s.name}
    # Track all slugs in memory for dedup during this sync run
    all_slugs: set[str] = {s.slug for s in all_schools if s.slug}

    # ── 4. Build contact index: Airtable school rec ID → list of contact recs ─
    contacts_by_school_id: dict[str, list[dict]] = {}
    for crec in at_contacts:
        fields = crec.get("fields", {})
        linked_schools: list[str] = fields.get("Sch") or []
        for sid in linked_schools:
            contacts_by_school_id.setdefault(sid, []).append(crec)

    # ── 5. Build existing contact dedup maps ──────────────────────────────────
    all_contacts: list[Contact] = db.execute(select(Contact)).scalars().all()
    # Primary dedup: airtable_id (sparse — most are null currently)
    contact_by_airtable_id: dict[str, Contact] = {c.airtable_id: c for c in all_contacts if c.airtable_id}
    # Fallback dedup: (school_id, normalized email)
    contact_by_school_email: dict[tuple, Contact] = {
        (c.school_id, (c.email or "").strip().lower()): c
        for c in all_contacts if c.email
    }

    schools_created = schools_updated = contacts_created = counselors_created = skipped = 0

    # ── 6. Process each Airtable school ──────────────────────────────────────
    for srec in at_schools:
        fields = srec.get("fields", {})
        airtable_rec_id: str = srec["id"]

        name: str | None = fields.get("School") or None
        at_slug: str | None = fields.get("slug") or None

        if not name:
            logger.warning("School record %s has no name — skipping", airtable_rec_id)
            skipped += 1
            continue

        # Resolve cohort from "Cohort 2" linked field (use first entry)
        cohort_links: list[str] = fields.get("Cohort 2") or []
        cohort = _resolve_cohort(cohort_links)
        is_customer = _parse_bool(fields.get("Current Customer"))

        # Check if school already exists (airtable_id → slug → name)
        existing = (
            school_by_airtable_id.get(airtable_rec_id)
            or (school_by_slug.get(at_slug) if at_slug else None)
            or school_by_name.get(name.strip().lower())
        )
        if existing:
            # Upsert: update mutable fields that may drift from Airtable
            if not existing.airtable_id:
                existing.airtable_id = airtable_rec_id
                school_by_airtable_id[airtable_rec_id] = existing
            if existing.is_current_customer != is_customer:
                existing.is_current_customer = is_customer
            new_cohort_id = cohort.id if cohort else None
            if existing.cohort_id != new_cohort_id:
                existing.cohort_id = new_cohort_id
            # Keep airtable_slug in sync with Airtable's latest value
            if existing.airtable_slug != at_slug:
                existing.airtable_slug = at_slug
            school = existing
            schools_updated += 1
        else:
            try:
                new_slug = at_slug if at_slug else unique_slug(name, all_slugs)
                school = School(
                    name=name,
                    airtable_id=airtable_rec_id,
                    street_address=fields.get("Street Address") or None,
                    city=fields.get("City") or None,
                    state=fields.get("State") or None,
                    zip_code=str(fields.get("Zip Code")).strip() if fields.get("Zip Code") else None,
                    enrollment_9_12=_parse_int(fields.get("Enrollment (9-12)")),
                    cmm_website_password=fields.get("CMM Website Password") or None,
                    school_resource_center_url=fields.get("School Resource Center URL") or None,
                    appointlet_link=fields.get("Appointlet Link") or None,
                    calendar_link=fields.get("Calendar Link") or None,
                    slug=new_slug,
                    airtable_slug=at_slug,
                    is_current_customer=is_customer,
                    cohort_id=cohort.id if cohort else None,
                )
                db.add(school)
                db.flush()  # get school.id before creating contacts
                school_by_airtable_id[airtable_rec_id] = school
                school_by_name[name.strip().lower()] = school
                school_by_slug[new_slug] = school
                all_slugs.add(new_slug)
                schools_created += 1
                logger.info("Created school: name=%s airtable_id=%s slug=%s", name, airtable_rec_id, new_slug)
            except Exception as exc:
                logger.error("Failed to create school %s (%s): %s", name, airtable_rec_id, exc)
                db.rollback()
                skipped += 1
                continue

        # ── 7. Sync contacts for this school (new and existing schools) ───────
        linked_contacts = contacts_by_school_id.get(airtable_rec_id, [])
        for crec in linked_contacts:
            cfields = crec.get("fields", {})
            contact_airtable_id: str = crec["id"]
            email: str | None = (cfields.get("Email") or "").strip() or None
            email_key = (school.id, email.lower()) if email else None

            # Dedup: airtable_id → (school_id, email)
            existing_contact = contact_by_airtable_id.get(contact_airtable_id)
            if not existing_contact and email_key:
                existing_contact = contact_by_school_email.get(email_key)
            contact_is_new = False
            if existing_contact:
                # Backfill airtable_id on existing contacts so future syncs use fast path
                if not existing_contact.airtable_id:
                    existing_contact.airtable_id = contact_airtable_id
                    contact_by_airtable_id[contact_airtable_id] = existing_contact
                contact = existing_contact
            else:
                contact_is_new = True
                try:
                    contact = Contact(
                        airtable_id=contact_airtable_id,
                        school_id=school.id,
                        first_name=cfields.get("First Name") or None,
                        last_name=cfields.get("Last Name") or None,
                        email=email,
                        role=cfields.get("Role") or None,
                        receive_comms=_parse_bool(cfields.get("Receive Comms")),
                        auto_emails=_parse_bool(cfields.get("Auto Emails")),
                        softr_access=_parse_bool(cfields.get("Softr Access")),
                    )
                    db.add(contact)
                    db.flush()
                    contact_by_airtable_id[contact_airtable_id] = contact
                    if email_key:
                        contact_by_school_email[email_key] = contact
                    contacts_created += 1
                    logger.info(
                        "Created contact: email=%s school=%s", email or "(no email)", name
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to create contact %s for school %s: %s",
                        email or contact_airtable_id,
                        name,
                        exc,
                    )
                    db.rollback()
                    continue

            # ── 8. Create Supabase auth user + counselor role for NEW contacts with email ──
            # Skip existing contacts — they already have Supabase users provisioned
            if not email or not contact_is_new:
                continue

            first_name: str = cfields.get("First Name") or ""
            last_name: str = cfields.get("Last Name") or ""

            try:
                resp = supabase.auth.admin.create_user(
                    {
                        "email": email,
                        "user_metadata": {
                            "first_name": first_name,
                            "last_name": last_name,
                        },
                        "email_confirm": True,
                    }
                )
                if not resp or not resp.user:
                    logger.error("Supabase create_user returned no user for %s", email)
                    continue
                new_user = resp.user
            except Exception as exc:
                error_msg = str(exc).lower()
                if "already" in error_msg or "exists" in error_msg or "registered" in error_msg:
                    # User already in Supabase Auth — look them up to assign role
                    logger.info("Supabase user already exists for %s, looking up", email)
                    try:
                        users_resp = supabase.auth.admin.list_users()
                        new_user = next(
                            (u for u in (users_resp or []) if u.email and u.email.lower() == email.lower()),
                            None,
                        )
                    except Exception as list_exc:
                        logger.error("list_users failed for %s: %s", email, list_exc)
                        new_user = None
                    if not new_user:
                        logger.warning("Could not locate existing Supabase user for %s — skipping role", email)
                        continue
                else:
                    logger.error("create_user failed for %s: %s", email, exc)
                    continue

            # Check if UserRole already exists for this user
            user_uuid = uuid.UUID(new_user.id)
            existing_role = db.query(UserRole).filter(UserRole.user_id == user_uuid).first()
            if existing_role:
                logger.info("UserRole already exists for user %s — skipping", email)
                continue

            try:
                user_role = UserRole(
                    user_id=user_uuid,
                    role="counselor",
                    school_id=school.id,
                )
                db.add(user_role)
                db.flush()
                counselors_created += 1
                logger.info("Created counselor role: email=%s school=%s", email, name)
            except Exception as exc:
                logger.error("Failed to create UserRole for %s: %s", email, exc)
                db.rollback()

    db.commit()
    synced_at = datetime.now(timezone.utc)
    logger.info(
        "Schools sync complete: schools_created=%d schools_updated=%d contacts_created=%d counselors_created=%d skipped=%d",
        schools_created,
        schools_updated,
        contacts_created,
        counselors_created,
        skipped,
    )
    return {
        "schools_created": schools_created,
        "schools_updated": schools_updated,
        "contacts_created": contacts_created,
        "counselors_created": counselors_created,
        "skipped": skipped,
        "synced_at": synced_at,
    }
