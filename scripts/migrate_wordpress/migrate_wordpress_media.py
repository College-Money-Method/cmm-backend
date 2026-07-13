#!/usr/bin/env python3
"""
Migrate WordPress media library files to S3 and rewrite content_assets.link references.

Steps:
  1. Fetch all media items from WordPress REST API (GET /wp-json/wp/v2/media)
  2. Query DB for all content_assets rows whose link points to WordPress
  3. For each non-image file, download it and upload to S3:
       - If the URL matches a content_assets row → resources/{asset_id}/{filename}
       - Otherwise (size variants not directly referenced) → wordpress-media/{filename}
  4. Build a mapping of {wp_source_url → s3_url}
  5. Update content_assets.link in the DB wherever a WP URL is found
  6. Insert rows into storage_files registry (ON CONFLICT DO UPDATE — idempotent re-runs)
  7. Print a summary

Usage (from project root):
  uv run python scripts/migrate_wordpress_media.py
  uv run python scripts/migrate_wordpress_media.py --dry-run
  uv run python scripts/migrate_wordpress_media.py --skip-download   # re-run DB rewrite only

Auth:
  Reads WORDPRESS_APPLICATION_PASSWORD from .env
  WP username is hardcoded as the site admin (vu.nguyen@collegemoneymethod.com)
"""

from __future__ import annotations

import argparse
import base64
import sys
import uuid
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlparse

import boto3
import requests

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

from src.config import settings
from src.db.base import get_engine

WP_DOMAIN = "https://collegemoneymethod.com"
WP_USER = "nnavu99@gmail.com"
WP_MEDIA_API = f"{WP_DOMAIN}/wp-json/wp/v2/media"


class UploadResult(TypedDict):
    s3_key: str
    s3_url: str
    original_filename: str
    extension: str | None
    mime_type: str
    file_size_bytes: int | None


def wp_auth_headers() -> dict:
    password = settings.wordpress_application_password
    if not password:
        print("[error] WORDPRESS_APPLICATION_PASSWORD is not set in .env", file=sys.stderr)
        sys.exit(1)
    creds = base64.b64encode(f"{WP_USER}:{password}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}


def fetch_all_media(headers: dict) -> list[dict]:
    """Paginate through /wp/v2/media and return all items."""
    all_items: list[dict] = []
    page = 1
    while True:
        resp = requests.get(
            WP_MEDIA_API,
            params={"per_page": 100, "page": page, "_fields": "id,source_url,mime_type,media_details"},
            headers=headers,
            timeout=30,
        )
        if resp.status_code == 400:
            break  # past last page
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        all_items.extend(batch)
        total_pages = int(resp.headers.get("X-WP-TotalPages", "1"))
        if page >= total_pages:
            break
        page += 1
    return all_items


def fetch_wp_asset_id_map(conn) -> dict[str, str]:
    """
    Query content_assets for all rows whose link points to WordPress.
    Returns {wp_url: asset_id_str}.
    """
    rows = conn.execute(
        text(
            "SELECT id::text, link FROM content_assets "
            "WHERE link LIKE '%collegemoneymethod.com/wp-content%'"
        )
    ).fetchall()
    return {row[1]: row[0] for row in rows}


def _parse_filename(source_url: str) -> tuple[str, str | None]:
    """Return (filename, extension | None) from a URL."""
    filename = Path(urlparse(source_url).path).name
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else None
    return filename, extension


def s3_key_for(source_url: str, asset_id: str | None) -> str:
    filename, _ = _parse_filename(source_url)
    if asset_id:
        return f"resources/{asset_id}/{filename}"
    return f"wordpress-media/{filename}"


def s3_url_for(s3_key: str) -> str:
    return f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{s3_key}"


def upload_to_s3(
    s3_client,
    source_url: str,
    asset_id: str | None,
    wp_mime_type: str,
    headers: dict,
    dry_run: bool,
) -> UploadResult | None:
    """Download from WP and upload to S3. Returns an UploadResult or None on error."""
    s3_key = s3_key_for(source_url, asset_id)
    s3_url = s3_url_for(s3_key)
    filename, extension = _parse_filename(source_url)

    if dry_run:
        print(f"    [dry-run] would upload → s3://{settings.s3_bucket_name}/{s3_key}")
        return UploadResult(
            s3_key=s3_key,
            s3_url=s3_url,
            original_filename=filename,
            extension=extension,
            mime_type=wp_mime_type or "application/octet-stream",
            file_size_bytes=None,
        )

    try:
        resp = requests.get(source_url, headers=headers, timeout=60)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", wp_mime_type or "application/octet-stream").split(";")[0].strip()
        s3_client.put_object(
            Bucket=settings.s3_bucket_name,
            Key=s3_key,
            Body=resp.content,
            ContentType=content_type,
        )
        return UploadResult(
            s3_key=s3_key,
            s3_url=s3_url,
            original_filename=filename,
            extension=extension,
            mime_type=content_type,
            file_size_bytes=len(resp.content),
        )
    except Exception as e:
        print(f"    [error] failed to upload {source_url}: {e}")
        return None


def rewrite_db_links(conn, url_map: dict[str, str], dry_run: bool) -> int:
    """Update content_assets.link for each wp→s3 URL pair. Returns count of rows updated."""
    updated = 0
    for wp_url, s3_url in url_map.items():
        if dry_run:
            count = conn.execute(
                text("SELECT COUNT(*) FROM content_assets WHERE link = :url"),
                {"url": wp_url},
            ).scalar()
            if count:
                print(f"    [dry-run] {count} row(s) with link={wp_url!r} → {s3_url}")
                updated += count
        else:
            result = conn.execute(
                text("UPDATE content_assets SET link = :new_url WHERE link = :old_url"),
                {"new_url": s3_url, "old_url": wp_url},
            )
            if result.rowcount:
                print(f"    updated {result.rowcount} row(s): {wp_url!r} → {s3_url}")
                updated += result.rowcount
    return updated


def register_storage_files(conn, records: list[UploadResult], dry_run: bool) -> int:
    """
    Upsert all uploaded files into storage_files.
    ON CONFLICT (s3_key) → update metadata so re-runs are idempotent.
    Returns number of rows inserted/updated.
    """
    if dry_run:
        print(f"    [dry-run] would upsert {len(records)} row(s) into storage_files")
        return len(records)

    upserted = 0
    for r in records:
        conn.execute(
            text(
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
            ),
            {
                "id": str(uuid.uuid4()),
                "s3_key": r["s3_key"],
                "s3_url": r["s3_url"],
                "original_filename": r["original_filename"],
                "extension": r["extension"],
                "mime_type": r["mime_type"],
                "file_size_bytes": r["file_size_bytes"],
            },
        )
        upserted += 1
    return upserted


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate WordPress media to S3")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    parser.add_argument(
        "--skip-download",
        action="store_true",
        help="Skip S3 upload (assumes files already exist); only rewrite DB links",
    )
    args = parser.parse_args()

    headers = wp_auth_headers()
    engine = get_engine()

    print(f"Fetching media from {WP_MEDIA_API} ...")
    media_items = fetch_all_media(headers)
    print(f"Found {len(media_items)} media items\n")

    print("Querying DB for existing WordPress link references ...")
    with engine.connect() as conn:
        wp_asset_id_map = fetch_wp_asset_id_map(conn)
    print(f"Found {len(wp_asset_id_map)} content_asset row(s) with WordPress links\n")

    s3_client = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )

    url_map: dict[str, str] = {}        # wp_url → s3_url (for DB link rewrite)
    storage_records: list[UploadResult] = []
    upload_ok = upload_err = 0

    for item in media_items:
        source_url: str = item.get("source_url", "")
        if not source_url:
            continue

        # Skip images — already migrated
        mime_type: str = item.get("mime_type", "")
        if mime_type.startswith("image/"):
            continue

        asset_id = wp_asset_id_map.get(source_url)
        print(f"  {source_url}")
        if asset_id:
            print(f"    matched content_asset {asset_id}")

        if args.skip_download:
            s3_key = s3_key_for(source_url, asset_id)
            s3_url = s3_url_for(s3_key)
            filename, extension = _parse_filename(source_url)
            url_map[source_url] = s3_url
            storage_records.append(UploadResult(
                s3_key=s3_key,
                s3_url=s3_url,
                original_filename=filename,
                extension=extension,
                mime_type=mime_type or "application/octet-stream",
                file_size_bytes=None,
            ))
            print(f"    [skip-download] mapped → {s3_url}")
            upload_ok += 1
        else:
            result = upload_to_s3(s3_client, source_url, asset_id, mime_type, headers, dry_run=args.dry_run)
            if result:
                url_map[source_url] = result["s3_url"]
                storage_records.append(result)
                upload_ok += 1
            else:
                upload_err += 1

        # Size variants (e.g. thumbnail, medium) — non-image media rarely have these,
        # but handle them for completeness
        sizes: dict = item.get("media_details", {}).get("sizes", {})
        for size_info in sizes.values():
            size_url: str = size_info.get("source_url", "")
            if not size_url or size_url == source_url:
                continue
            size_asset_id = wp_asset_id_map.get(size_url)
            if args.skip_download:
                size_key = s3_key_for(size_url, size_asset_id)
                size_s3_url = s3_url_for(size_key)
                fname, ext = _parse_filename(size_url)
                url_map[size_url] = size_s3_url
                storage_records.append(UploadResult(
                    s3_key=size_key,
                    s3_url=size_s3_url,
                    original_filename=fname,
                    extension=ext,
                    mime_type=mime_type or "application/octet-stream",
                    file_size_bytes=None,
                ))
            else:
                r = upload_to_s3(s3_client, size_url, size_asset_id, mime_type, headers, dry_run=args.dry_run)
                if r:
                    url_map[size_url] = r["s3_url"]
                    storage_records.append(r)

    print(f"\nUploaded: {upload_ok}, Errors: {upload_err}")
    print(f"\nRewriting {len(url_map)} DB link(s) in content_assets ...")
    print(f"Registering {len(storage_records)} file(s) in storage_files ...\n")

    with engine.begin() as conn:
        db_updated = rewrite_db_links(conn, url_map, dry_run=args.dry_run)
        sf_upserted = register_storage_files(conn, storage_records, dry_run=args.dry_run)

    print(f"\nDone: {upload_ok} files migrated, {db_updated} DB link(s) updated, "
          f"{sf_upserted} storage_files row(s) upserted, {upload_err} errors")
    if args.dry_run:
        print("(dry run — nothing was written)")
    return 0 if upload_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
