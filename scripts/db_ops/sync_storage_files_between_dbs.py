#!/usr/bin/env python3
"""
Copy `storage_files` registry rows from a SOURCE database to a TARGET database.

Why: dev and prod share the same S3 bucket (cmm-general), but the WordPress-media
bulk import only registered `storage_files` rows in prod. Dev therefore has the
files in S3 but no registry rows, so the resource→storage linking scripts have
nothing to match against. This copies the rows over (the s3_url values are valid
in both environments because the bucket is shared).

Idempotent: upserts on the unique `s3_key`, so re-runs are safe.

Reads each DB's connection string straight from its env file's DATABASE_URL via
dotenv (without mutating the process environment), so it can talk to both DBs in
one run regardless of the cached app settings.

Usage (from project root):
  uv run python scripts/sync_storage_files_between_dbs.py --source-env .env --target-env .env.dev --dry-run
  uv run python scripts/sync_storage_files_between_dbs.py --source-env .env --target-env .env.dev
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import dotenv_values
from sqlalchemy import create_engine, text

COLUMNS = ("s3_key", "s3_url", "original_filename", "extension", "mime_type", "file_size_bytes")


def db_url(env_file: str) -> str:
    path = Path(env_file)
    if not path.is_absolute():
        path = PROJECT_ROOT / env_file
    if not path.exists():
        raise SystemExit(f"[error] env file not found: {path}")
    url = (dotenv_values(path) or {}).get("DATABASE_URL")
    if not url:
        raise SystemExit(f"[error] DATABASE_URL not set in {path}")
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="Copy storage_files rows between databases")
    parser.add_argument("--source-env", required=True, help="env file for the SOURCE db (e.g. .env)")
    parser.add_argument("--target-env", required=True, help="env file for the TARGET db (e.g. .env.dev)")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    src_engine = create_engine(db_url(args.source_env))
    dst_engine = create_engine(db_url(args.target_env))

    with src_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT s3_key, s3_url, original_filename, extension, mime_type, file_size_bytes "
            "FROM storage_files ORDER BY s3_key"
        )).mappings().all()

    print(f"Source has {len(rows)} storage_files row(s) "
          f"({args.source_env} → {args.target_env})\n")
    if not rows:
        print("Nothing to copy.")
        return 0

    if args.dry_run:
        for r in rows[:10]:
            print(f"  [dry-run] {r['s3_key']}")
        if len(rows) > 10:
            print(f"  … and {len(rows) - 10} more")
        print(f"\n(dry run — would upsert {len(rows)} row(s) into target)")
        return 0

    inserted = 0
    with dst_engine.begin() as conn:
        for r in rows:
            conn.execute(text(
                """
                INSERT INTO storage_files
                    (id, s3_key, s3_url, original_filename, extension, mime_type, file_size_bytes)
                VALUES
                    (:id, :s3_key, :s3_url, :original_filename, :extension, :mime_type, :file_size_bytes)
                ON CONFLICT (s3_key) DO UPDATE SET
                    s3_url            = EXCLUDED.s3_url,
                    original_filename = EXCLUDED.original_filename,
                    extension         = EXCLUDED.extension,
                    mime_type         = EXCLUDED.mime_type,
                    file_size_bytes   = COALESCE(EXCLUDED.file_size_bytes, storage_files.file_size_bytes)
                """
            ), {"id": str(uuid.uuid4()), **{k: r[k] for k in COLUMNS}})
            inserted += 1

    print(f"Done: upserted {inserted} row(s) into target.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
