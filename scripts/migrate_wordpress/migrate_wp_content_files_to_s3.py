#!/usr/bin/env python3
"""
Repoint content_assets whose `link` still points at WordPress wp-content uploads
(PDFs, spreadsheets, etc.) to the new S3 files repository.

Unlike scripts/migrate_wordpress_media.py, this does NOT rely on the WordPress
media REST API (which missed rows because of www/non-www host mismatches). It
downloads each file directly from the URL stored in content_assets.link.

For every content_assets row where link LIKE '%collegemoneymethod.com/wp-content%':
  1. Download the file directly from its current link URL.
  2. Upload it to S3 at resources/{asset_id}/{filename}.
  3. Upsert a storage_files registry row (idempotent on s3_key).
  4. Update content_assets.link to the new S3 URL.

Usage (from project root):
  uv run python scripts/migrate_wp_content_files_to_s3.py --dry-run
  uv run python scripts/migrate_wp_content_files_to_s3.py
"""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from urllib.parse import unquote, urlparse

import boto3
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

from src.config import settings
from src.db.base import get_engine

WP_CONTENT_PATTERN = "%collegemoneymethod.com/wp-content%"


def parse_filename(url: str) -> tuple[str, str | None]:
    filename = unquote(Path(urlparse(url).path).name)
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else None
    return filename, extension


def s3_url_for(s3_key: str) -> str:
    return f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate WP wp-content files to S3")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    args = parser.parse_args()

    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT id::text, name, link FROM content_assets "
                "WHERE link LIKE :pat ORDER BY name"
            ),
            {"pat": WP_CONTENT_PATTERN},
        ).fetchall()

    print(f"Found {len(rows)} content_asset(s) with WordPress wp-content links\n")
    if not rows:
        return 0

    s3_client = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )

    ok = err = 0
    for asset_id, name, link in rows:
        filename, extension = parse_filename(link)
        s3_key = f"resources/{asset_id}/{filename}"
        s3_url = s3_url_for(s3_key)
        print(f"  {name}\n    {link}")

        if args.dry_run:
            print(f"    [dry-run] → {s3_url}\n")
            ok += 1
            continue

        try:
            resp = requests.get(link, timeout=60)
            resp.raise_for_status()
            content_type = (
                resp.headers.get("Content-Type", "application/octet-stream")
                .split(";")[0]
                .strip()
            )
            s3_client.put_object(
                Bucket=settings.s3_bucket_name,
                Key=s3_key,
                Body=resp.content,
                ContentType=content_type,
            )
        except Exception as e:  # noqa: BLE001
            print(f"    [error] {e}\n")
            err += 1
            continue

        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO storage_files
                        (id, s3_key, s3_url, original_filename, extension, mime_type, file_size_bytes)
                    VALUES
                        (:id, :s3_key, :s3_url, :fn, :ext, :mt, :sz)
                    ON CONFLICT (s3_key) DO UPDATE SET
                        s3_url            = EXCLUDED.s3_url,
                        original_filename = EXCLUDED.original_filename,
                        extension         = EXCLUDED.extension,
                        mime_type         = EXCLUDED.mime_type,
                        file_size_bytes   = COALESCE(EXCLUDED.file_size_bytes, storage_files.file_size_bytes)
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "s3_key": s3_key,
                    "s3_url": s3_url,
                    "fn": filename,
                    "ext": extension,
                    "mt": content_type,
                    "sz": len(resp.content),
                },
            )
            conn.execute(
                text("UPDATE content_assets SET link = :new WHERE id = :id"),
                {"new": s3_url, "id": asset_id},
            )
        print(f"    uploaded ({len(resp.content)} bytes) → {s3_url}\n")
        ok += 1

    print(f"Done: {ok} migrated, {err} error(s)")
    if args.dry_run:
        print("(dry run — nothing was written)")
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
