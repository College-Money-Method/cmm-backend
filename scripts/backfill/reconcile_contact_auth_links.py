"""One-time reconciliation: fix cross-wired contact->auth-user links and retire
duplicate contact rows created by Airtable delete+recreate (new airtable_id).

Background (see docs/airtable-auth-reconciliation-plan.md): a past mis-assignment
left ~51 contacts pointing at the WRONG Supabase login (contact.email !=
auth.users.email), and ~44 duplicate rows (same email, dead airtable_id) exist.

Rules:
  - Correct login for a contact = auth.users row whose email == contact.email.
  - Per email group, KEEP the row whose airtable_id is in the live Airtable pull
    (tie-break: already-correct link, else first). Relink KEEP to its correct login.
  - Retire the other rows in the group: soft-delete (deleted_at) + release user_id.
  - Contacts with NULL user_id are left for normal provisioning (not touched here).
  - Contacts with no matching login (e.g. manual/test users) are skipped + logged.
  - profiles mirror is rebuilt for every affected user_id.

Usage:
    python -m scripts.backfill.reconcile_contact_auth_links            # dry-run
    python -m scripts.backfill.reconcile_contact_auth_links --apply    # write
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

import src.main  # noqa: F401 — load the full SQLAlchemy model registry

from sqlalchemy import text
from src.auth.profile_sync import delete_profile, upsert_profile
from src.db.base import get_session_factory
from src.integrations.airtable import get_contacts_records

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("reconcile")


def build_plan(db, pull_ids: set[str]):
    contacts = db.execute(text(
        "SELECT id::text id, lower(email) email, airtable_id, user_id::text user_id, "
        "first_name, last_name FROM contacts WHERE email IS NOT NULL"
    )).all()
    users = db.execute(text(
        "SELECT id::text id, lower(email) email FROM auth.users WHERE email IS NOT NULL"
    )).all()
    login_by_email = {u.email: u.id for u in users}

    groups: dict[str, list] = defaultdict(list)
    for c in contacts:
        groups[c.email].append(c)

    fix_wrong, soft_deletes, no_login = [], [], []
    for email, rows in groups.items():
        correct = login_by_email.get(email)
        keep = (next((r for r in rows if r.airtable_id in pull_ids), None)
                or next((r for r in rows if r.user_id == correct), None) or rows[0])
        for r in rows:
            if r.id != keep.id:
                soft_deletes.append(r)
        if not correct:
            no_login.append(keep)
        elif keep.user_id and keep.user_id != correct:
            fix_wrong.append((keep, correct))
        # keep.user_id is None -> leave for provisioning; == correct -> already ok
    return fix_wrong, soft_deletes, no_login


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    args = ap.parse_args()

    pull_ids = {r["id"] for r in get_contacts_records()}
    db = get_session_factory()()
    try:
        fix_wrong, soft_deletes, no_login = build_plan(db, pull_ids)
        log.info("FIX-WRONG relinks: %d | SOFT-DELETE dup rows: %d | skipped(no login): %d",
                 len(fix_wrong), len(soft_deletes), len(no_login))
        for keep, correct in fix_wrong[:5]:
            log.info("  relink %s: %s -> %s", keep.email, (keep.user_id or "")[:8], correct[:8])
        for r in no_login:
            log.info("  SKIP (no login): %s", r.email)

        if not args.apply:
            log.info("DRY-RUN only. Re-run with --apply to write.")
            return

        now = datetime.now(timezone.utc)
        affected: set[str] = set()
        # Logins currently on the duplicate rows we're about to retire. Any that
        # end up unreferenced (a person's stale 2nd login) get their role revoked.
        dup_logins: set[str] = {r.user_id for r in soft_deletes if r.user_id}

        # 1) Retire duplicate rows: soft-delete + release user_id (frees logins).
        for r in soft_deletes:
            if r.user_id:
                affected.add(r.user_id)
            db.execute(text(
                "UPDATE contacts SET deleted_at=:now, user_id=NULL WHERE id=:id"
            ), {"now": now, "id": r.id})
        db.flush()

        # 2) Relink corrupted links. Null first (avoid uq_contacts_user_id
        #    collisions across the swap permutation), then set targets.
        for keep, correct in fix_wrong:
            if keep.user_id:
                affected.add(keep.user_id)
            affected.add(correct)
            db.execute(text("UPDATE contacts SET user_id=NULL WHERE id=:id"), {"id": keep.id})
        db.flush()
        for keep, correct in fix_wrong:
            db.execute(text("UPDATE contacts SET user_id=:uid WHERE id=:id"),
                       {"uid": correct, "id": keep.id})
        db.flush()

        # 2b) Revoke roles for duplicate logins that are now unreferenced by any
        #     active contact (soft: drop UserRole + profile, KEEP the auth user).
        #     Never touch super_admin or admin-created (still-referenced) logins.
        revoked_orphans = 0
        for uid in dup_logins:
            affected.add(uid)
            referenced = db.execute(text(
                "SELECT 1 FROM contacts WHERE user_id=:u AND deleted_at IS NULL LIMIT 1"
            ), {"u": uid}).first()
            if referenced:
                continue
            role = db.execute(text("SELECT role FROM user_roles WHERE user_id=:u"), {"u": uid}).first()
            if role and role.role != "super_admin":
                db.execute(text("DELETE FROM user_roles WHERE user_id=:u"), {"u": uid})
                revoked_orphans += 1
        db.flush()

        # 3) Rebuild profiles for every affected login from its current contact.
        for uid in affected:
            row = db.execute(text(
                "SELECT lower(email) email, first_name, last_name FROM contacts "
                "WHERE user_id=:uid AND deleted_at IS NULL LIMIT 1"
            ), {"uid": uid}).first()
            if row:
                upsert_profile(db, uid, row.email, row.first_name, row.last_name)
            else:
                delete_profile(db, uid)

        db.commit()
        log.info("APPLIED. affected logins: %d | orphan roles revoked: %d",
                 len(affected), revoked_orphans)
    finally:
        db.close()


if __name__ == "__main__":
    main()
