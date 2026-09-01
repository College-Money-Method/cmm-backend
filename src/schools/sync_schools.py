"""Airtable → DB sync for schools only (contacts handled by sync_contacts)."""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.cycles.models import Cohort
from src.integrations.airtable import get_cohorts_records, get_schools_records
from src.schools.models import School
from src.schools.slug_utils import unique_slug
from src.schools.sync_utils import parse_bool, parse_int

logger = logging.getLogger(__name__)


def sync_schools_from_airtable(db: Session) -> dict:
    """
    Upsert schools from Airtable (Airtable is source of truth).

    - Create new schools; update mutable fields on existing ones
    - Match order: airtable_id → slug → name
    - Per-record errors don't abort the sync

    Returns {"schools_created", "schools_updated", "skipped"}
    """
    at_schools = get_schools_records()
    at_cohorts = get_cohorts_records()

    # Cohort lookups: Airtable rec ID → DB Cohort, with name fallback
    all_cohorts: list[Cohort] = db.execute(select(Cohort)).scalars().all()
    cohort_by_airtable_id: dict[str, Cohort] = {c.airtable_id: c for c in all_cohorts if c.airtable_id}
    cohort_by_name: dict[str, Cohort] = {c.name: c for c in all_cohorts}
    at_cohort_name_by_id: dict[str, str] = {
        rec["id"]: (rec.get("fields", {}).get("Name") or "")
        for rec in at_cohorts
    }

    def _resolve_cohort(cohort_rec_ids: list[str]) -> Cohort | None:
        """Return the first DB cohort matching any given Airtable rec ID."""
        for cid in cohort_rec_ids:
            cohort = cohort_by_airtable_id.get(cid)
            if cohort:
                return cohort
            name = at_cohort_name_by_id.get(cid)
            if name:
                cohort = cohort_by_name.get(name)
                if cohort:
                    return cohort
        return None

    # School lookup maps (existing DB schools)
    all_schools: list[School] = db.execute(select(School)).scalars().all()
    school_by_airtable_id: dict[str, School] = {s.airtable_id: s for s in all_schools if s.airtable_id}
    school_by_slug: dict[str, School] = {s.slug: s for s in all_schools if s.slug}
    school_by_name: dict[str, School] = {s.name.strip().lower(): s for s in all_schools if s.name}
    all_slugs: set[str] = {s.slug for s in all_schools if s.slug}

    # Every record ID present in this pull — used to tell a *dead* airtable_id on a
    # DB row (Airtable record deleted/recreated) from one another live record owns.
    pulled_airtable_ids: set[str] = {srec["id"] for srec in at_schools}

    schools_created = schools_updated = skipped = cohorts_unresolved = 0
    airtable_ids_refreshed = 0

    for srec in at_schools:
        fields = srec.get("fields", {})
        airtable_rec_id: str = srec["id"]

        name: str | None = fields.get("School") or None
        at_slug: str | None = fields.get("slug") or None

        if not name:
            logger.warning("School record %s has no name — skipping", airtable_rec_id)
            skipped += 1
            continue

        cohort_links: list[str] = fields.get("Cohort 2") or []
        cohort = _resolve_cohort(cohort_links)
        # ISSUE-4: surface silent NULL cohort links (e.g. cohorts not yet synced)
        if cohort_links and cohort is None:
            cohorts_unresolved += 1
            logger.warning(
                "School %s (%s) has cohort link(s) %s that resolve to no DB cohort",
                name, airtable_rec_id, cohort_links,
            )
        is_customer = parse_bool(fields.get("Current Customer"))

        # Parse mutable fields once — applied to both create and update paths
        street_address = fields.get("Street Address") or None
        city = fields.get("City") or None
        state = fields.get("State") or None
        zip_code = str(fields.get("Zip Code")).strip() if fields.get("Zip Code") else None
        enrollment_9_12 = parse_int(fields.get("Enrollment (9-12)"))
        cmm_website_password = fields.get("CMM Website Password") or None
        school_resource_center_url = fields.get("School Resource Center URL") or None
        appointlet_link = fields.get("Appointlet Link") or None
        calendar_link = fields.get("Calendar Link") or None
        new_cohort_id = cohort.id if cohort else None

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
            elif existing.airtable_id != airtable_rec_id:
                # Matched by slug/name while carrying a different airtable_id. When
                # the stored ID is absent from this pull, the Airtable record was
                # deleted and recreated, leaving the DB row pointing at a dead ID —
                # which silently breaks every airtable_id-keyed sync (webinar →
                # portal_mapping, contacts → school). Refresh it. Guarded twice:
                # the stored ID must be dead, and the new ID must be unclaimed, so
                # duplicate-named Airtable records can never steal each other's ID.
                if (
                    existing.airtable_id not in pulled_airtable_ids
                    and airtable_rec_id not in school_by_airtable_id
                ):
                    logger.warning(
                        "Refreshed stale airtable_id for school %s: %s → %s "
                        "(old record no longer in Airtable)",
                        name, existing.airtable_id, airtable_rec_id,
                    )
                    school_by_airtable_id.pop(existing.airtable_id, None)
                    existing.airtable_id = airtable_rec_id
                    school_by_airtable_id[airtable_rec_id] = existing
                    airtable_ids_refreshed += 1
                else:
                    logger.warning(
                        "School %s matched by slug/name but airtable_id mismatch left "
                        "unchanged: db=%s airtable=%s (ID collision or both live)",
                        name, existing.airtable_id, airtable_rec_id,
                    )
            if existing.name != name:
                existing.name = name
            if existing.street_address != street_address:
                existing.street_address = street_address
            if existing.city != city:
                existing.city = city
            if existing.state != state:
                existing.state = state
            if existing.zip_code != zip_code:
                existing.zip_code = zip_code
            if existing.enrollment_9_12 != enrollment_9_12:
                existing.enrollment_9_12 = enrollment_9_12
            if existing.cmm_website_password != cmm_website_password:
                existing.cmm_website_password = cmm_website_password
            if existing.school_resource_center_url != school_resource_center_url:
                existing.school_resource_center_url = school_resource_center_url
            if existing.appointlet_link != appointlet_link:
                existing.appointlet_link = appointlet_link
            if existing.calendar_link != calendar_link:
                existing.calendar_link = calendar_link
            if existing.is_current_customer != is_customer:
                existing.is_current_customer = is_customer
            if existing.cohort_id != new_cohort_id:
                existing.cohort_id = new_cohort_id
            if existing.airtable_slug != at_slug:
                # Airtable slug changed. `slug` owns the public URL now, so only
                # propagate when it still tracks the old Airtable value — an
                # admin-customized slug is never overwritten. Skip if the new
                # slug is already claimed (unique constraint).
                if (
                    at_slug
                    and existing.slug == existing.airtable_slug
                    and at_slug not in all_slugs
                ):
                    all_slugs.discard(existing.slug)
                    school_by_slug.pop(existing.slug, None)
                    existing.slug = at_slug
                    all_slugs.add(at_slug)
                    school_by_slug[at_slug] = existing
                existing.airtable_slug = at_slug
            schools_updated += 1
            continue

        try:
            new_slug = at_slug if at_slug else unique_slug(name, all_slugs)
            school = School(
                name=name,
                airtable_id=airtable_rec_id,
                street_address=street_address,
                city=city,
                state=state,
                zip_code=zip_code,
                enrollment_9_12=enrollment_9_12,
                cmm_website_password=cmm_website_password,
                school_resource_center_url=school_resource_center_url,
                appointlet_link=appointlet_link,
                calendar_link=calendar_link,
                slug=new_slug,
                airtable_slug=at_slug,
                is_current_customer=is_customer,
                cohort_id=new_cohort_id,
            )
            db.add(school)
            db.flush()
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

    db.commit()
    logger.info(
        "Schools sync complete: created=%d updated=%d skipped=%d cohorts_unresolved=%d "
        "airtable_ids_refreshed=%d",
        schools_created, schools_updated, skipped, cohorts_unresolved, airtable_ids_refreshed,
    )
    return {
        "schools_created": schools_created,
        "schools_updated": schools_updated,
        "cohorts_unresolved": cohorts_unresolved,
        "airtable_ids_refreshed": airtable_ids_refreshed,
        "skipped": skipped,
    }
