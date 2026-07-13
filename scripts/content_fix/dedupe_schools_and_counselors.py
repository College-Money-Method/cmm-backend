#!/usr/bin/env python3
"""Merge duplicate schools (and their duplicated counselors/contacts) into one canonical row.

Background
----------
The Airtable school sync (`src/schools/sync.py`) dedupes incoming records by
`airtable_id` -> `slug` -> normalized `name`. Rows that predate the `airtable_id`
column have `airtable_id = NULL`; when the slug/name fallbacks miss, the sync
inserts a *new* row instead of updating the old one. Result: two `schools` rows
for the same school — the old one holding the real data (sales, the counselor
`user_roles`, original contacts) and the new one holding the `airtable_id` plus a
duplicate contact.

This script finds those duplicate groups and merges them. The CANONICAL (kept)
row is the one carrying a non-null `airtable_id` (so future syncs keep matching
it); if no row in a group has an `airtable_id`, the oldest row wins. All child
rows (sales, user_roles, meetings, etc.) are repointed from the loser rows to the
canonical row, duplicate contacts (same email) are collapsed, and the loser
`schools` rows are deleted.

Child tables are discovered dynamically from Postgres FK metadata, so any future
table referencing `schools.id` is handled automatically.

Safety
------
- Dry-run by default. Pass --apply to commit. Everything runs in ONE transaction.
- Idempotent: re-running after a successful merge is a no-op.

Usage (from project root):
    uv run python scripts/dedupe_schools_and_counselors.py            # dry-run
    uv run python scripts/dedupe_schools_and_counselors.py --apply    # commit
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure project root is on sys.path so `src.*` imports work.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.db.base import get_session_factory

# Tables handled with custom logic — excluded from the blanket repoint.
SPECIAL_CHILD_TABLES = {"contacts"}


def discover_child_tables(db: Session) -> list[tuple[str, str]]:
    """Return [(table, column), ...] for every FK referencing schools.id."""
    rows = db.execute(text("""
        SELECT rel.relname AS child_table, att.attname AS child_column
        FROM pg_constraint con
        JOIN pg_class rel  ON rel.oid = con.conrelid
        JOIN pg_class frel ON frel.oid = con.confrelid
        JOIN unnest(con.conkey) WITH ORDINALITY AS ck(attnum, ord) ON true
        JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ck.attnum
        WHERE con.contype = 'f' AND frel.relname = 'schools'
        ORDER BY child_table
    """)).all()
    return [(r.child_table, r.child_column) for r in rows]


def find_duplicate_groups(db: Session) -> list[list[dict]]:
    """Return groups (by normalized name) that have >1 schools row."""
    rows = db.execute(text("""
        SELECT id, name, slug, airtable_id, created_at,
               lower(trim(name)) AS norm
        FROM schools
        WHERE name IS NOT NULL
        ORDER BY lower(trim(name)), airtable_id NULLS LAST, created_at
    """)).mappings().all()

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["norm"], []).append(dict(r))
    return [g for g in groups.values() if len(g) > 1]


def pick_canonical(group: list[dict]) -> dict:
    """Canonical = the single airtable_id row, else the oldest row.

    If multiple rows carry distinct airtable_ids they are genuinely separate
    Airtable records — not a sync duplicate — so we keep the oldest and let the
    caller skip (handled in merge_group).
    """
    with_atid = [s for s in group if s["airtable_id"]]
    if len(with_atid) == 1:
        return with_atid[0]
    # 0 or >1 airtable_ids -> fall back to oldest by created_at
    return sorted(group, key=lambda s: s["created_at"])[0]


def merge_group(db: Session, group: list[dict], child_tables: list[tuple[str, str]]) -> dict:
    """Merge one duplicate group into its canonical row. Returns a stats dict."""
    name = group[0]["name"]
    distinct_atids = {s["airtable_id"] for s in group if s["airtable_id"]}
    if len(distinct_atids) > 1:
        print(f"  SKIP '{name}': {len(distinct_atids)} distinct airtable_ids "
              f"(separate Airtable records, not a sync duplicate)")
        return {"skipped": 1}

    canonical = pick_canonical(group)
    losers = [s for s in group if s["id"] != canonical["id"]]
    cid = canonical["id"]

    print(f"  MERGE '{name}': keep {cid} (airtable_id={canonical['airtable_id']}), "
          f"merge {len(losers)} loser(s)")

    stats = {"contacts_moved": 0, "contacts_removed": 0, "rows_repointed": 0, "schools_deleted": 0}

    for loser in losers:
        lid = loser["id"]

        # --- contacts: dedup by lower(email) against the canonical row ---
        loser_contacts = db.execute(text("""
            SELECT id, lower(trim(email)) AS em, airtable_id
            FROM contacts WHERE school_id = :lid
        """), {"lid": lid}).mappings().all()

        for c in loser_contacts:
            if c["em"]:
                dup = db.execute(text("""
                    SELECT id FROM contacts
                    WHERE school_id = :cid AND lower(trim(email)) = :em
                    LIMIT 1
                """), {"cid": cid, "em": c["em"]}).first()
                if dup:
                    db.execute(text("DELETE FROM contacts WHERE id = :id"), {"id": c["id"]})
                    stats["contacts_removed"] += 1
                    print(f"      - removed duplicate contact {c['id']} (email={c['em']})")
                    continue
            db.execute(text("UPDATE contacts SET school_id = :cid WHERE id = :id"),
                       {"cid": cid, "id": c["id"]})
            stats["contacts_moved"] += 1

        # --- all other child tables: blanket repoint loser -> canonical ---
        for table, column in child_tables:
            if table in SPECIAL_CHILD_TABLES:
                continue
            res = db.execute(
                text(f"UPDATE {table} SET {column} = :cid WHERE {column} = :lid"),
                {"cid": cid, "lid": lid},
            )
            if res.rowcount:
                stats["rows_repointed"] += res.rowcount
                print(f"      ~ repointed {res.rowcount} row(s) in {table}")

        # --- delete the now-empty loser school ---
        db.execute(text("DELETE FROM schools WHERE id = :lid"), {"lid": lid})
        stats["schools_deleted"] += 1
        print(f"      x deleted loser school {lid}")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Commit changes (default is dry-run).")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"=== Dedupe schools & counselors [{mode}] ===\n")

    SessionLocal = get_session_factory()
    db = SessionLocal()
    try:
        child_tables = discover_child_tables(db)
        print("Child tables referencing schools.id:")
        for t, c in child_tables:
            print(f"  - {t}.{c}")
        print()

        groups = find_duplicate_groups(db)
        if not groups:
            print("No duplicate school groups found. Nothing to do.")
            return

        print(f"Found {len(groups)} duplicate school group(s):\n")
        totals = {"contacts_moved": 0, "contacts_removed": 0,
                  "rows_repointed": 0, "schools_deleted": 0, "skipped": 0}
        for group in groups:
            s = merge_group(db, group, child_tables)
            for k, v in s.items():
                totals[k] = totals.get(k, 0) + v
            print()

        print("=== Summary ===")
        print(f"  Schools deleted:   {totals['schools_deleted']}")
        print(f"  Contacts moved:    {totals['contacts_moved']}")
        print(f"  Contacts removed:  {totals['contacts_removed']}  (duplicated counselors)")
        print(f"  Child rows moved:  {totals['rows_repointed']}")
        print(f"  Groups skipped:    {totals['skipped']}")

        if args.apply:
            db.commit()
            print("\nCommitted.")
        else:
            db.rollback()
            print("\nDry-run only — no changes committed. Re-run with --apply to commit.")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
