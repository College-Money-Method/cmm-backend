"""Nuke all non-super_admin counselor accounts and re-provision cleanly from
Airtable. Use when contact<->auth links have diverged too far to reconcile.

Steps (--apply):
  1. Delete ALL survey_responses (avoid dangling user_id references).
  2. Delete non-super_admin user_roles + profiles.
  3. NULL contacts.user_id (so provisioning re-resolves every contact by email).
  4. Delete non-super_admin Supabase auth users (admin API — cascades GoTrue state).
  5. Run the full Airtable sync -> fresh, correct accounts (deterministic passwords).

super_admin accounts are always preserved.

Usage:
  uv run --env-file=.env.local python -m scripts.backfill.nuke_and_resync_counselors          # dry-run
  uv run --env-file=.env.local python -m scripts.backfill.nuke_and_resync_counselors --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import src.main  # noqa: F401 — full model registry

from sqlalchemy import bindparam, text
from src.config import settings
from src.db.client import get_supabase
from src.db.deps import get_session_factory
from src.schools.sync import sync_schools_contacts_from_airtable

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("nuke")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    db = get_session_factory()()
    supabase = get_supabase()
    try:
        super_ids = [str(r[0]) for r in db.execute(
            text("SELECT user_id FROM user_roles WHERE role='super_admin'")
        ).all()]
        surveys = db.execute(text("SELECT count(*) FROM survey_responses")).scalar()
        sales = db.execute(text("SELECT count(*) FROM sales")).scalar()
        roles = db.execute(text("SELECT count(*) FROM user_roles WHERE role<>'super_admin'")).scalar()
        auth_users = db.execute(text("SELECT count(*) FROM auth.users")).scalar()
        contacts = db.execute(text("SELECT count(*) FROM contacts")).scalar()

        log.info("TARGET DB host: %s", urlparse(settings.database_url).hostname)
        log.info("super_admins preserved: %d", len(super_ids))
        log.info("WILL delete: survey_responses=%d, sales=%d, non-super user_roles=%d, "
                 "auth.users=%d (minus %d super), ALL contacts=%d (rebuilt from Airtable)",
                 surveys, sales, roles, auth_users - len(super_ids), len(super_ids), contacts)

        if not args.apply:
            log.info("DRY-RUN only. Re-run with --apply.")
            return

        # 1-3) DB wipes (single transaction). Contacts are 100% Airtable-derived,
        # so we delete them wholesale and let the sync rebuild clean rows (fresh
        # ids, no stale/duplicate carry-over). sales cleared first (FK → contacts).
        db.execute(text("DELETE FROM survey_responses"))
        db.execute(text("DELETE FROM sales"))
        db.execute(text("DELETE FROM user_roles WHERE role<>'super_admin'"))
        if super_ids:
            db.execute(
                text("DELETE FROM profiles WHERE user_id NOT IN :ids")
                .bindparams(bindparam("ids", tuple(super_ids), expanding=True))
            )
        else:
            db.execute(text("DELETE FROM profiles"))
        db.execute(text("DELETE FROM contacts"))
        db.commit()
        log.info("DB wipe committed (contacts fully cleared).")

        # 4) Delete non-super auth users via admin API.
        super_set = set(super_ids)
        ids = [str(r[0]) for r in db.execute(text("SELECT id FROM auth.users")).all()
               if str(r[0]) not in super_set]
        deleted = 0
        for uid in ids:
            try:
                supabase.auth.admin.delete_user(uid)
                deleted += 1
                if deleted % 50 == 0:
                    log.info("  deleted %d/%d auth users…", deleted, len(ids))
            except Exception as exc:
                log.error("  delete_user %s failed: %s", uid, exc)
        log.info("Deleted %d/%d auth users.", deleted, len(ids))

        # 5) Rebuild from Airtable. Loop until stable: on a dirty legacy dataset
        #    (duplicate rows + stale airtable_ids) the first pass misses a handful
        #    of contacts that resolve on the next pass. Once canonical the sync is
        #    fully idempotent, so this converges in 2-3 passes.
        result = {}
        for i in range(1, 4):
            result = sync_schools_contacts_from_airtable(db, supabase)
            log.info("sync pass %d: contacts_created=%d deactivated=%d counselors_created=%d skipped=%d",
                     i, result["contacts_created"], result["contacts_deactivated"],
                     result["counselors_created"], result["skipped"])
            if (result["contacts_created"] == 0 and result["contacts_deactivated"] == 0
                    and result["counselors_created"] == 0):
                log.info("Converged after pass %d.", i)
                break
        log.info("FINAL SYNC RESULT: %s", {k: v for k, v in result.items()})
    finally:
        db.close()


if __name__ == "__main__":
    main()
