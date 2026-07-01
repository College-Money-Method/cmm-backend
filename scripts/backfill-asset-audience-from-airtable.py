#!/usr/bin/env python3
"""
Backfill for_counselor and for_family on existing content_assets from Airtable (live).

Fetches the Assets table from AIRTABLE_ASSET_BASE_ID and updates each matching
content_assets row by airtable_id.

Usage (from project root):
  uv run python scripts/backfill-asset-audience-from-airtable.py
  uv run python scripts/backfill-asset-audience-from-airtable.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyairtable import Api

from src.config import settings
from src.db.base import get_engine
from sqlalchemy import text


def _parse_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("true", "1", "yes", "checked")


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill for_counselor/for_family from Airtable live data")
    parser.add_argument("--dry-run", action="store_true", help="Print updates without writing to DB")
    args = parser.parse_args()

    if not settings.airtable_api_key:
        print("Error: AIRTABLE_API_KEY is not set", file=sys.stderr)
        return 1
    if not settings.airtable_asset_base_id:
        print("Error: AIRTABLE_ASSET_BASE_ID is not set", file=sys.stderr)
        return 1

    print("Fetching Assets from Airtable...")
    api = Api(settings.airtable_api_key)
    records = api.table(settings.airtable_asset_base_id, "Assets").all()
    print(f"  {len(records)} records fetched")

    rows = []
    for rec in records:
        fields = rec.get("fields", {})
        counselor_raw = fields.get("Counselor")
        family_raw = fields.get("Family")
        rows.append({
            "airtable_id": rec["id"],
            # Default to True when field is absent (same as server_default)
            "for_counselor": _parse_bool(counselor_raw) if counselor_raw is not None else True,
            "for_family": _parse_bool(family_raw) if family_raw is not None else False,
        })

    counselor_only = sum(1 for r in rows if r["for_counselor"] and not r["for_family"])
    family_only = sum(1 for r in rows if r["for_family"] and not r["for_counselor"])
    both = sum(1 for r in rows if r["for_counselor"] and r["for_family"])
    neither = sum(1 for r in rows if not r["for_counselor"] and not r["for_family"])
    print(f"  counselor-only: {counselor_only}, family-only: {family_only}, both: {both}, neither: {neither}")

    if args.dry_run:
        print("\nDry run — no changes written.")
        return 0

    updated = not_found = 0
    with get_engine().begin() as conn:
        for row in rows:
            result = conn.execute(
                text(
                    """
                    UPDATE content_assets
                       SET for_counselor = :for_counselor,
                           for_family    = :for_family
                     WHERE airtable_id = :airtable_id
                    """
                ),
                row,
            )
            if result.rowcount:
                updated += 1
            else:
                not_found += 1
                print(f"  [warn] airtable_id not found in DB: {row['airtable_id']}")

    print(f"Done — {updated} updated, {not_found} not found in DB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
