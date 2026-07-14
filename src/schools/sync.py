"""Airtable → DB sync orchestrators.

Pipeline (one direction, no drift):
    Airtable ──> schools          (sync_schools)
    Airtable ──> contacts         (sync_contacts — source of truth for counselors)
    contacts ──> auth users/roles (sync_provisioning — derived, writes user_id back)

The public entry points below preserve the original function names/response
shapes used by the routers.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.cycles.sync import sync_cohorts_from_airtable
from src.schools.sync_contacts import sync_contacts_from_airtable
from src.schools.sync_provisioning import provision_counselors_from_contacts
from src.schools.sync_schools import sync_schools_from_airtable

__all__ = [
    "sync_schools_contacts_from_airtable",
    "sync_counselors_from_airtable",
    "sync_contacts_from_airtable",
    "sync_schools_from_airtable",
    "provision_counselors_from_contacts",
]


def sync_schools_contacts_from_airtable(db: Session, supabase: object) -> dict:
    """Full sync: cohorts → schools → contacts → counselor provisioning.

    Cohorts run first so school→cohort links resolve without manual ordering.
    """
    sync_cohorts_from_airtable(db)
    schools_result = sync_schools_from_airtable(db)
    contacts_result = sync_contacts_from_airtable(db)
    provision_result = provision_counselors_from_contacts(db, supabase)
    return {
        "schools_created": schools_result["schools_created"],
        "schools_updated": schools_result["schools_updated"],
        "cohorts_unresolved": schools_result["cohorts_unresolved"],
        "contacts_created": contacts_result["contacts_created"],
        "contacts_updated": contacts_result["contacts_updated"],
        "contacts_unlinked": contacts_result["contacts_unlinked"],
        "contacts_deactivated": contacts_result["contacts_deactivated"],
        "contacts_reactivated": contacts_result["contacts_reactivated"],
        "email_collisions": contacts_result["email_collisions"],
        "counselors_created": provision_result["counselors_created"],
        "school_roles_updated": provision_result["school_roles_updated"],
        "counselors_revoked": provision_result["counselors_revoked"],
        "drift_relinked": provision_result["drift_relinked"],
        "skipped": schools_result["skipped"] + contacts_result["skipped"] + provision_result["skipped"],
        "synced_at": datetime.now(timezone.utc),
    }


def sync_counselors_from_airtable(db: Session, supabase: object) -> dict:
    """Counselor sync: refresh contacts from Airtable, then provision auth accounts."""
    contacts_result = sync_contacts_from_airtable(db)
    provision_result = provision_counselors_from_contacts(db, supabase)
    return {
        "contacts_created": contacts_result["contacts_created"],
        "contacts_updated": contacts_result["contacts_updated"],
        "contacts_deactivated": contacts_result["contacts_deactivated"],
        "contacts_reactivated": contacts_result["contacts_reactivated"],
        "email_collisions": contacts_result["email_collisions"],
        "counselors_created": provision_result["counselors_created"],
        "school_roles_updated": provision_result["school_roles_updated"],
        "counselors_revoked": provision_result["counselors_revoked"],
        "drift_relinked": provision_result["drift_relinked"],
        "skipped": contacts_result["skipped"] + provision_result["skipped"],
        "synced_at": datetime.now(timezone.utc).isoformat(),
    }
