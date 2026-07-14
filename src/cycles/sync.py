"""Airtable → DB sync logic for cohorts."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.cycles.models import Cohort
from src.integrations.airtable import get_cohorts_records

logger = logging.getLogger(__name__)


def sync_cohorts_from_airtable(db: Session) -> dict:
    """
    Pull all Airtable cohort records and create new cohorts in DB.

    Matching strategy (create-only, never updates existing):
      1. By name (unique in DB) — primary key for business dedup
      2. By airtable_id (fast path on subsequent runs)

    Stores airtable_id and hide_unavailability_calendar on newly created cohorts.
    Returns {"created": N, "skipped": N, "synced_at": ...}
    """
    records = get_cohorts_records()

    all_cohorts: list[Cohort] = db.execute(select(Cohort)).scalars().all()
    by_airtable_id: dict[str, Cohort] = {c.airtable_id: c for c in all_cohorts if c.airtable_id}
    by_name: dict[str, Cohort] = {c.name: c for c in all_cohorts}

    created = updated = skipped = 0

    for rec in records:
        fields = rec.get("fields", {})
        airtable_rec_id: str = rec["id"]
        name: str | None = fields.get("Name") or None

        if not name:
            logger.warning("Cohort record %s has no Name — skipping", airtable_rec_id)
            skipped += 1
            continue

        hide_cal_raw = fields.get("Hide Unavailability Calendar")
        hide_unavailability_calendar: bool = bool(hide_cal_raw) if hide_cal_raw is not None else False

        # ISSUE-6: upsert editable fields on match. `name` stays create-only —
        # it's the business dedup key, so renames are handled deliberately elsewhere.
        existing = by_airtable_id.get(airtable_rec_id) or by_name.get(name)
        if existing:
            changed = False
            if not existing.airtable_id:
                existing.airtable_id = airtable_rec_id
                changed = True
            if existing.hide_unavailability_calendar != hide_unavailability_calendar:
                existing.hide_unavailability_calendar = hide_unavailability_calendar
                changed = True
            if changed:
                updated += 1
            else:
                skipped += 1
            continue

        try:
            # SAVEPOINT: catch integrity errors per record without rolling back
            # the whole batch (a plain db.rollback() would undo all prior flushes).
            with db.begin_nested():
                cohort = Cohort(
                    name=name,
                    airtable_id=airtable_rec_id,
                    hide_unavailability_calendar=hide_unavailability_calendar,
                )
                db.add(cohort)
                db.flush()
            by_name[name] = cohort
            by_airtable_id[airtable_rec_id] = cohort
            created += 1
            logger.info("Created cohort: name=%s airtable_id=%s", name, airtable_rec_id)
        except Exception as exc:
            logger.error("Failed to create cohort %s (%s): %s", name, airtable_rec_id, exc)
            skipped += 1

    db.commit()
    synced_at = datetime.now(timezone.utc)
    logger.info("Cohort sync complete: created=%d updated=%d skipped=%d", created, updated, skipped)
    return {"created": created, "updated": updated, "skipped": skipped, "synced_at": synced_at}
