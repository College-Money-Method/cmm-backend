"""Fetch fresh school logos from Airtable, generate thumbnails, and store in S3.

Airtable attachment URLs expire ~1 hour after generation, so this script
fetches fresh records from Airtable at runtime instead of using the stale
URLs stored in the DB.

S3 paths:
  assets/schools/<slug>/logo.<ext>       — full image (original format)
  assets/schools/<slug>/logo-thumb.webp  — 200px thumbnail (WEBP)

Both logo_url and logo_thumb_url are updated in the schools table.

Usage:
    uv run python scripts/migrate_logos_to_s3.py             # all schools
    uv run python scripts/migrate_logos_to_s3.py --dry-run   # preview only
    uv run python scripts/migrate_logos_to_s3.py --school "Vail Mountain"

Requires AIRTABLE_API_KEY, AIRTABLE_BASE_ID, AWS credentials, and
s3_bucket_name in .env.
"""

from __future__ import annotations

import argparse
import io
import re
import sys
from pathlib import Path

import requests
from PIL import Image
from pyairtable import Api

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# Must import all models so SQLAlchemy can resolve cross-model relationships
import src.assets.models  # noqa: F401
import src.auth.models  # noqa: F401
import src.calendar.models  # noqa: F401
import src.cycles.models  # noqa: F401
import src.meetings.models  # noqa: F401
import src.sales.models  # noqa: F401
import src.settings.models  # noqa: F401
import src.workshops.models  # noqa: F401

from src.config import settings
from src.db.deps import get_db
from src.schools.models import School
from src.storage.s3_client import s3_client

AIRTABLE_TABLE = "Schools"
AIRTABLE_LOGO_FIELD = "Logo"
THUMB_SIZE = 200
THUMB_QUALITY = 85


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slugify(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[''']", "", slug)
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


def _ext_from_content_type(ct: str) -> str:
    return {
        "image/webp": "webp",
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
    }.get(ct.split(";")[0].strip(), "webp")


def _s3_public_url(key: str) -> str:
    return f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{key}"


def _upload(s3, key: str, data: bytes, content_type: str, dry_run: bool) -> str:
    url = _s3_public_url(key)
    size_kb = len(data) // 1024
    if dry_run:
        print(f"    [dry-run] Would upload → s3://{settings.s3_bucket_name}/{key}  ({size_kb} KB)")
    else:
        s3.put_object(
            Bucket=settings.s3_bucket_name,
            Key=key,
            Body=data,
            ContentType=content_type,
        )
        print(f"    Uploaded → {key}  ({size_kb} KB)")
    return url


# ---------------------------------------------------------------------------
# Fetch fresh Airtable records
# ---------------------------------------------------------------------------

def fetch_airtable_logos(api_key: str, base_id: str) -> dict[str, str]:
    """
    Return a dict mapping school name (lowercase) → fresh logo URL.
    Fetches all Schools records from Airtable.
    """
    print("Fetching fresh logo URLs from Airtable...")
    api = Api(api_key)
    records = api.table(base_id, AIRTABLE_TABLE).all(fields=[AIRTABLE_LOGO_FIELD, "School"])
    logo_map: dict[str, str] = {}
    for rec in records:
        fields = rec.get("fields", {})
        name = fields.get("School", "").strip()
        attachments = fields.get(AIRTABLE_LOGO_FIELD)
        if name and attachments and isinstance(attachments, list) and attachments:
            url = attachments[0].get("url")
            if url:
                logo_map[name.lower()] = url
    print(f"Found logos for {len(logo_map)} school(s) in Airtable\n")
    return logo_map


# ---------------------------------------------------------------------------
# Core migration
# ---------------------------------------------------------------------------

def migrate_school(
    school: School,
    logo_url: str,
    s3,
    dry_run: bool,
) -> tuple[str, str] | None:
    slug = school.slug or _slugify(school.name)

    print(f"  Downloading logo...")
    try:
        resp = requests.get(logo_url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  ERROR downloading: {exc}")
        return None

    content_type = resp.headers.get("content-type", "image/webp")
    ext = _ext_from_content_type(content_type)
    raw_bytes = resp.content
    print(f"  Downloaded {len(raw_bytes) // 1024} KB  ({content_type})")

    # Full logo
    logo_key = f"assets/schools/{slug}/logo.{ext}"
    new_logo_url = _upload(s3, logo_key, raw_bytes, content_type, dry_run)

    # Thumbnail via Pillow
    try:
        img = Image.open(io.BytesIO(raw_bytes)).convert("RGBA")
        img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[3])
        buf = io.BytesIO()
        background.save(buf, format="WEBP", quality=THUMB_QUALITY)
        thumb_bytes = buf.getvalue()
        thumb_size = background.size
    except Exception as exc:
        print(f"  ERROR generating thumbnail: {exc}")
        return None

    thumb_key = f"assets/schools/{slug}/logo-thumb.webp"
    new_thumb_url = _upload(s3, thumb_key, thumb_bytes, "image/webp", dry_run)
    print(f"  Thumbnail: {thumb_size[0]}x{thumb_size[1]}px  ({len(thumb_bytes) // 1024} KB)")

    return new_logo_url, new_thumb_url


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate school logos to S3 via Airtable")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--school", metavar="NAME", help="Process a single school (partial name match)")
    args = parser.parse_args()

    for var, val in [
        ("AIRTABLE_API_KEY", settings.airtable_api_key),
        ("AIRTABLE_BASE_ID", settings.airtable_base_id),
        ("s3_bucket_name", settings.s3_bucket_name),
    ]:
        if not val:
            print(f"ERROR: {var} is not set in .env")
            sys.exit(1)

    # Fetch fresh URLs from Airtable
    logo_map = fetch_airtable_logos(settings.airtable_api_key, settings.airtable_base_id)

    s3 = s3_client()
    db = next(get_db())

    query = db.query(School)
    if args.school:
        query = query.filter(School.name.ilike(f"%{args.school}%"))
    schools = query.order_by(School.name).all()

    # Match DB schools to Airtable logos
    to_process = [(s, logo_map.get(s.name.lower())) for s in schools]
    has_logo = [(s, url) for s, url in to_process if url]
    no_match = [s for s, url in to_process if not url]

    print(f"{'[DRY RUN] ' if args.dry_run else ''}"
          f"{len(schools)} school(s) queried — "
          f"{len(has_logo)} have Airtable logos, "
          f"{len(no_match)} not found in Airtable\n")

    if no_match:
        print("No Airtable logo match for:")
        for s in no_match:
            print(f"  {s.name}")
        print()

    updated = failed = 0
    for school, logo_url in has_logo:
        slug = school.slug or _slugify(school.name)
        print(f"[{has_logo.index((school, logo_url)) + 1}/{len(has_logo)}] {school.name}  (slug: {slug})")
        result = migrate_school(school, logo_url, s3, args.dry_run)
        if result:
            new_logo_url, new_thumb_url = result
            if not args.dry_run:
                school.logo_url = new_logo_url
                school.logo_thumb_url = new_thumb_url
                db.commit()
            updated += 1
        else:
            failed += 1
        print()

    print("─" * 60)
    print(f"Done.  Updated: {updated}  Failed: {failed}  No Airtable match: {len(no_match)}")


if __name__ == "__main__":
    main()
