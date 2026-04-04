"""Migrate workshop art and content asset images from Airtable to S3.

The airtable_pull_data script stored Airtable attachment objects (JSON) in
text columns. Those URLs expire after ~1 hour, so this script:
  1. Fetches fresh attachment URLs from Airtable (keyed by airtable_id)
  2. Downloads each image
  3. Uploads to S3
  4. Updates the DB column with the permanent S3 URL

S3 paths:
  assets/workshops/<airtable_id>/<filename>
  assets/content/<airtable_id>/<filename>

Usage:
    uv run python scripts/migrate_images_to_s3.py                   # all
    uv run python scripts/migrate_images_to_s3.py --dry-run         # preview
    uv run python scripts/migrate_images_to_s3.py --only workshops  # workshops only
    uv run python scripts/migrate_images_to_s3.py --only content    # content only

Requires AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AWS credentials, and
s3_bucket_name in .env.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import requests
from pyairtable import Api

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Import all models so SQLAlchemy can resolve relationships
import src.assets.models  # noqa: F401
import src.auth.models  # noqa: F401
import src.calendar.models  # noqa: F401
import src.content.models  # noqa: F401
import src.cycles.models  # noqa: F401
import src.meetings.models  # noqa: F401
import src.sales.models  # noqa: F401
import src.schools.models  # noqa: F401
import src.settings.models  # noqa: F401
import src.workshops.models  # noqa: F401

from src.config import settings
from src.content.models import ContentAsset
from src.db.deps import get_db
from src.storage.s3_client import s3_client
from src.workshops.models import Workshop


# ---------------------------------------------------------------------------
# Airtable table / field config
# ---------------------------------------------------------------------------

AIRTABLE_WORKSHOPS_TABLE = "Workshops"
AIRTABLE_WORKSHOPS_ART_FIELD = "Workshop Art"

AIRTABLE_CONTENT_TABLE = "Content Assets"
AIRTABLE_CONTENT_IMAGE_FIELD = "Image"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[''']", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _s3_public_url(key: str) -> str:
    return f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{key}"


def _sanitize_filename(name: str) -> str:
    """Make a filename safe for S3 keys."""
    name = re.sub(r"[^\w.\-]", "_", name)
    return name.lower()


def _ext_from_content_type(ct: str) -> str:
    return {
        "image/webp": "webp",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/svg+xml": "svg",
    }.get(ct.split(";")[0].strip(), "jpg")


def _upload(s3, key: str, data: bytes, content_type: str, dry_run: bool) -> str:
    url = _s3_public_url(key)
    size_kb = len(data) // 1024
    if dry_run:
        print(f"    [dry-run] Would upload -> s3://{settings.s3_bucket_name}/{key}  ({size_kb} KB)")
    else:
        s3.put_object(
            Bucket=settings.s3_bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        print(f"    Uploaded -> {key}  ({size_kb} KB)")
    return url


def _is_s3_url(url: str | None) -> bool:
    """Check if a URL is already an S3 URL (already migrated)."""
    if not url:
        return False
    return ".s3." in url and "amazonaws.com" in url


def _is_airtable_json(val: str | None) -> bool:
    """Check if a value looks like a stringified Airtable attachment JSON."""
    if not val:
        return False
    val = val.strip()
    return val.startswith("{") or val.startswith("[")


def _parse_airtable_url_from_json(val: str) -> str | None:
    """Try to extract a URL from a stringified Airtable attachment."""
    try:
        parsed = json.loads(val)
        if isinstance(parsed, dict):
            return parsed.get("url")
        if isinstance(parsed, list) and parsed:
            return parsed[0].get("url") if isinstance(parsed[0], dict) else None
    except (json.JSONDecodeError, TypeError):
        return None
    return None


# ---------------------------------------------------------------------------
# Fetch fresh Airtable attachment URLs
# ---------------------------------------------------------------------------

def fetch_airtable_attachments(
    api_key: str,
    base_id: str,
    table_name: str,
    field_name: str,
    name_field: str | None = None,
) -> dict[str, dict]:
    """
    Return a dict mapping key -> first attachment info dict.
    If name_field is provided, key is the lowercased name; otherwise key is airtable_id.
    Each attachment dict has: url, filename, type, etc.
    """
    print(f"Fetching fresh attachment URLs from Airtable ({table_name}.{field_name})...")
    api = Api(api_key)
    fetch_fields = [field_name]
    if name_field:
        fetch_fields.append(name_field)
    records = api.table(base_id, table_name).all(fields=fetch_fields)
    attachment_map: dict[str, dict] = {}
    for rec in records:
        attachments = rec.get("fields", {}).get(field_name)
        if attachments and isinstance(attachments, list) and attachments:
            if name_field:
                name = rec.get("fields", {}).get(name_field, "").strip()
                if name:
                    attachment_map[name.lower()] = attachments[0]
            else:
                attachment_map[rec["id"]] = attachments[0]
    print(f"  Found {len(attachment_map)} record(s) with attachments\n")
    return attachment_map


# ---------------------------------------------------------------------------
# Migration logic
# ---------------------------------------------------------------------------

def migrate_workshop_art(s3, db, dry_run: bool) -> tuple[int, int]:
    """Migrate workshop art images. Returns (updated, failed)."""
    print("=" * 60)
    print("WORKSHOP ART IMAGES")
    print("=" * 60)

    # Workshop model has no airtable_id, so we match by name
    attachment_map = fetch_airtable_attachments(
        settings.airtable_api_key,
        settings.airtable_base_id,
        AIRTABLE_WORKSHOPS_TABLE,
        AIRTABLE_WORKSHOPS_ART_FIELD,
        name_field="Name",
    )

    workshops = db.query(Workshop).all()
    print(f"Found {len(workshops)} workshop(s) in DB\n")

    updated = failed = skipped = 0
    for ws in workshops:
        # Skip if already migrated to S3
        if _is_s3_url(ws.workshop_art_url):
            skipped += 1
            continue

        attachment = attachment_map.get(ws.name.lower())
        if not attachment:
            print(f"  [{ws.name}] No Airtable attachment found, skipping")
            skipped += 1
            continue

        fresh_url = attachment.get("url")
        filename = attachment.get("filename", "art.jpg")
        if not fresh_url:
            print(f"  [{ws.name}] No URL in attachment, skipping")
            skipped += 1
            continue

        print(f"  [{ws.name}] Downloading {filename}...")
        try:
            resp = requests.get(fresh_url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            print(f"    ERROR downloading: {exc}")
            failed += 1
            continue

        content_type = resp.headers.get("content-type", "image/jpeg")
        ext = _ext_from_content_type(content_type)
        safe_name = _sanitize_filename(Path(filename).stem)
        ws_slug = _slugify(ws.name)
        s3_key = f"assets/workshops/{ws_slug}/{safe_name}.{ext}"

        new_url = _upload(s3, s3_key, resp.content, content_type, dry_run)

        if not dry_run:
            ws.workshop_art_url = new_url
            db.commit()
        updated += 1

    print(f"\nWorkshops: {updated} updated, {failed} failed, {skipped} skipped\n")
    return updated, failed


def migrate_content_images(s3, db, dry_run: bool) -> tuple[int, int]:
    """Migrate content asset images. Returns (updated, failed)."""
    print("=" * 60)
    print("CONTENT ASSET IMAGES")
    print("=" * 60)

    # Content assets live in a separate Airtable base
    content_base_id = settings.airtable_asset_base_id or settings.airtable_base_id
    attachment_map = fetch_airtable_attachments(
        settings.airtable_api_key,
        content_base_id,
        AIRTABLE_CONTENT_TABLE,
        AIRTABLE_CONTENT_IMAGE_FIELD,
    )

    assets = db.query(ContentAsset).filter(ContentAsset.airtable_id.isnot(None)).all()
    print(f"Found {len(assets)} content asset(s) with airtable_id\n")

    updated = failed = skipped = 0
    for asset in assets:
        # Skip if already migrated to S3
        if _is_s3_url(asset.image_url):
            skipped += 1
            continue

        attachment = attachment_map.get(asset.airtable_id)
        if not attachment:
            # No image in Airtable for this asset
            skipped += 1
            continue

        fresh_url = attachment.get("url")
        filename = attachment.get("filename", "image.jpg")
        if not fresh_url:
            skipped += 1
            continue

        print(f"  [{asset.name[:50]}] Downloading {filename}...")
        try:
            resp = requests.get(fresh_url, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            print(f"    ERROR downloading: {exc}")
            failed += 1
            continue

        content_type = resp.headers.get("content-type", "image/jpeg")
        ext = _ext_from_content_type(content_type)
        safe_name = _sanitize_filename(Path(filename).stem)
        s3_key = f"assets/content/{asset.airtable_id}/{safe_name}.{ext}"

        new_url = _upload(s3, s3_key, resp.content, content_type, dry_run)

        if not dry_run:
            asset.image_url = new_url
            db.commit()
        updated += 1

    print(f"\nContent assets: {updated} updated, {failed} failed, {skipped} skipped\n")
    return updated, failed


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate Airtable attachment images to S3"
    )
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument(
        "--only",
        choices=["workshops", "content"],
        help="Only migrate one type",
    )
    args = parser.parse_args()

    for var, val in [
        ("AIRTABLE_API_KEY", settings.airtable_api_key),
        ("AIRTABLE_BASE_ID", settings.airtable_base_id),
        ("s3_bucket_name", settings.s3_bucket_name),
    ]:
        if not val:
            print(f"ERROR: {var} is not set in .env")
            sys.exit(1)

    s3 = s3_client()
    db = next(get_db())

    total_updated = total_failed = 0

    if args.only != "content":
        u, f = migrate_workshop_art(s3, db, args.dry_run)
        total_updated += u
        total_failed += f

    if args.only != "workshops":
        u, f = migrate_content_images(s3, db, args.dry_run)
        total_updated += u
        total_failed += f

    print("=" * 60)
    print(f"DONE.  Total updated: {total_updated}  Total failed: {total_failed}")
    if args.dry_run:
        print("(dry run — no changes written)")


if __name__ == "__main__":
    main()
