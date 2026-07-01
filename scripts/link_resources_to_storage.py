#!/usr/bin/env python3
"""
Repoint content_assets whose `link` is a non-S3 file URL to the matching file in
our S3 storage, matched DETERMINISTICALLY by filename.

The WordPress→S3 migration preserved original filenames, so
`storage_files.original_filename` equals the filename in the asset's current
link URL. For every asset whose link ends in a document extension and is not
already on S3, we look up the storage_files row with the same filename and set
`link` to its s3_url. When the same filename exists under several keys we prefer
the canonical `wordpress-media/` copy.

Assets with no link (or a non-file link) are left untouched and reported — they
carry no filename to match on.

Idempotent (assets already on S3 are not candidates) and environment-agnostic
(operates on whatever DB the loaded env points at).

Usage (from project root):
  uv run python scripts/link_resources_to_storage.py --dry-run
  uv run python scripts/link_resources_to_storage.py
  uv run python scripts/link_resources_to_storage.py --env-file .env.dev --dry-run
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

DOC_EXTENSIONS = ("pdf", "xlsx", "xls", "pptx", "ppt", "docx", "doc", "csv", "txt")


def filename_of(url: str) -> str:
    """Lowercased, URL-decoded final path segment of a URL."""
    return unquote(Path(urlparse(url).path).name).lower()


def main() -> int:
    parser = argparse.ArgumentParser(description="Repoint asset links to S3 by filename")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--env-file", help="dotenv to load before connecting (e.g. .env.dev)")
    args = parser.parse_args()

    if args.env_file:
        from dotenv import load_dotenv
        env_path = Path(args.env_file)
        if not env_path.is_absolute():
            env_path = PROJECT_ROOT / args.env_file
        if not env_path.exists():
            print(f"[error] env file not found: {env_path}")
            return 1
        load_dotenv(env_path, override=True)
        print(f"Loaded env from {env_path}")

    from src.db.base import get_engine

    engine = get_engine()
    ext_pattern = r"\.(" + "|".join(DOC_EXTENSIONS) + r")(\?|$)"

    with engine.connect() as conn:
        # Candidates: link is a document file URL that is not already on S3.
        assets = conn.execute(text("""
            SELECT id::text, name, link
            FROM content_assets
            WHERE link ~* :ext AND link NOT ILIKE '%amazonaws.com%'
            ORDER BY name
        """), {"ext": ext_pattern}).fetchall()

        # filename → s3_url, preferring the canonical wordpress-media/ copy.
        files = conn.execute(text(
            "SELECT lower(original_filename) AS fn, s3_url, s3_key FROM storage_files"
        )).fetchall()
        no_link = conn.execute(text(
            "SELECT count(*) FROM content_assets WHERE link IS NULL OR link = ''"
        )).scalar()

    by_name: dict[str, str] = {}
    for fn, s3_url, s3_key in files:
        if fn not in by_name or s3_key.startswith("wordpress-media/"):
            by_name[fn] = s3_url

    print(f"File-link candidates: {len(assets)} | storage files: {len(files)} | "
          f"no-link assets (skipped, no filename): {no_link}\n")

    updated = unmatched = skipped = 0
    with engine.begin() as conn:
        for asset_id, name, link in assets:
            target = by_name.get(filename_of(link))
            if not target:
                print(f"  [unmatched] {name}\n              {link}")
                unmatched += 1
                continue
            if target == link:
                skipped += 1
                continue
            print(f"  {name}\n    {link}\n    → {target}")
            if not args.dry_run:
                conn.execute(
                    text("UPDATE content_assets SET link = :u, updated_at = now() WHERE id = :id"),
                    {"u": target, "id": asset_id},
                )
            updated += 1
        if args.dry_run:
            conn.rollback()

    print(f"\nDone: {updated} repointed, {unmatched} file-link w/o storage match, {skipped} already-linked")
    if args.dry_run:
        print("(dry run — nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
