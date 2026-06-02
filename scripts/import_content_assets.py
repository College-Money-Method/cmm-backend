#!/usr/bin/env python3
"""
Import content assets from the Airtable asset base into Postgres.

Imports three tables in order:
  1. asset_types  – lookup table with S3 icon upload
  2. objectives   – linked to existing workshops by name
  3. content_assets – full content records with S3 image/file upload,
                      linked to asset_types, objectives, and workshops

Prerequisites:
  .env must have:
    AIRTABLE_API_KEY, AIRTABLE_ASSET_BASE_ID
    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, S3_BUCKET_NAME, AWS_REGION
    DATABASE_URL

Usage (from project root):
  uv run python scripts/import_content_assets.py
  uv run python scripts/import_content_assets.py --dry-run
  uv run python scripts/import_content_assets.py --reset
  uv run python scripts/import_content_assets.py --table asset_types
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pyairtable import Api

from src.config import settings
from src.db.base import get_engine
from sqlalchemy import text

# ── S3 ────────────────────────────────────────────────────────────────────────

def _s3():
    import boto3
    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    )


def _s3_public_url(key: str) -> str:
    return f"https://{settings.s3_bucket_name}.s3.{settings.aws_region}.amazonaws.com/{key}"


def _download(url: str) -> tuple[bytes, str]:
    """Download a URL; returns (bytes, content_type)."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "application/octet-stream")


def _ext_from_content_type(ct: str) -> str:
    mapping = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/svg+xml": "svg",
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.ms-excel": "xls",
    }
    return mapping.get(ct.split(";")[0].strip(), "bin")


def _upload_attachment(
    s3_client,
    airtable_attachment: dict[str, Any],
    s3_key: str,
    dry_run: bool,
) -> str | None:
    """Download an Airtable attachment and upload to S3. Returns public S3 URL."""
    url = airtable_attachment.get("url")
    if not url:
        return None
    try:
        data, content_type = _download(url)
    except Exception as e:
        print(f"    [warn] failed to download {url[:80]}: {e}")
        return None

    if dry_run:
        print(f"    [dry-run] would upload {len(data)} bytes → s3://{settings.s3_bucket_name}/{s3_key}")
        return _s3_public_url(s3_key)

    s3_client.put_object(
        Bucket=settings.s3_bucket_name,
        Key=s3_key,
        Body=data,
        ContentType=content_type,
    )
    return _s3_public_url(s3_key)


# ── Airtable helpers ──────────────────────────────────────────────────────────

def _fetch_table(api: Api, base_id: str, table_name: str) -> list[dict[str, Any]]:
    table = api.table(base_id, table_name)
    return table.all()


def _first_attachment(value: Any) -> dict[str, Any] | None:
    """Parse first attachment from an Airtable attachment field (JSON string or list)."""
    if not value:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            return None
    if isinstance(value, list) and value:
        return value[0]
    if isinstance(value, dict):
        return value
    return None


# ── Import phases ─────────────────────────────────────────────────────────────

def import_asset_types(
    conn,
    records: list[dict],
    s3_client,
    dry_run: bool,
) -> dict[str, uuid.UUID]:
    """Returns {airtable_id: db_uuid}."""
    id_map: dict[str, uuid.UUID] = {}
    inserted = skipped = 0

    for rec in records:
        airtable_id = rec["id"]
        fields = rec.get("fields", {})
        name = fields.get("Title") or fields.get("Asset Type") or ""
        if not name:
            print(f"  [skip] asset_type {airtable_id}: no name")
            skipped += 1
            continue

        color = fields.get("Color") or None

        # Upload icon to S3
        icon_attachment = _first_attachment(fields.get("Icon"))
        icon_url: str | None = None
        if icon_attachment:
            ext = _ext_from_content_type(icon_attachment.get("type", ""))
            s3_key = f"assets/content-types/{airtable_id}/icon.{ext}"
            icon_url = _upload_attachment(s3_client, icon_attachment, s3_key, dry_run)

        row_id = uuid.uuid4()
        id_map[airtable_id] = row_id

        if not dry_run:
            conn.execute(
                text(
                    """
                    INSERT INTO asset_types (id, airtable_id, name, color, icon_url)
                    VALUES (:id, :airtable_id, :name, :color, :icon_url)
                    ON CONFLICT (airtable_id) DO UPDATE
                        SET name = EXCLUDED.name,
                            color = EXCLUDED.color,
                            icon_url = COALESCE(EXCLUDED.icon_url, asset_types.icon_url)
                    """
                ),
                {
                    "id": str(row_id),
                    "airtable_id": airtable_id,
                    "name": name,
                    "color": color,
                    "icon_url": icon_url,
                },
            )
        inserted += 1

    print(f"  asset_types: {inserted} upserted, {skipped} skipped")
    return id_map


def import_objectives(
    conn,
    records: list[dict],
    workshop_name_to_id: dict[str, uuid.UUID],
    dry_run: bool,
) -> dict[str, uuid.UUID]:
    """Returns {airtable_id: db_uuid}."""
    id_map: dict[str, uuid.UUID] = {}
    inserted = skipped = workshop_links = 0

    for rec in records:
        airtable_id = rec["id"]
        fields = rec.get("fields", {})
        name = fields.get("Objective") or ""
        if not name:
            print(f"  [skip] objective {airtable_id}: no name")
            skipped += 1
            continue

        description = fields.get("Objective Description") or None
        row_id = uuid.uuid4()
        id_map[airtable_id] = row_id

        if not dry_run:
            conn.execute(
                text(
                    """
                    INSERT INTO objectives (id, airtable_id, name, description)
                    VALUES (:id, :airtable_id, :name, :description)
                    ON CONFLICT (airtable_id) DO UPDATE
                        SET name = EXCLUDED.name,
                            description = EXCLUDED.description
                    """
                ),
                {
                    "id": str(row_id),
                    "airtable_id": airtable_id,
                    "name": name,
                    "description": description,
                },
            )

            # Link to workshops by name
            workshop_names_raw = fields.get("Name (from Workshops)", "")
            if isinstance(workshop_names_raw, list):
                workshop_names = workshop_names_raw
            else:
                workshop_names = [
                    n.strip()
                    for n in str(workshop_names_raw).split(",")
                    if n.strip()
                ]

            for wname in workshop_names:
                wid = workshop_name_to_id.get(wname)
                if wid:
                    conn.execute(
                        text(
                            """
                            INSERT INTO objective_workshops (objective_id, workshop_id)
                            VALUES (:oid, :wid)
                            ON CONFLICT DO NOTHING
                            """
                        ),
                        {"oid": str(row_id), "wid": str(wid)},
                    )
                    workshop_links += 1
                else:
                    if wname:
                        print(f"    [warn] objective '{name}': workshop not found: '{wname}'")

        inserted += 1

    print(f"  objectives: {inserted} upserted, {skipped} skipped, {workshop_links} workshop links")
    return id_map


def import_content_assets(
    conn,
    records: list[dict],
    asset_type_id_map: dict[str, uuid.UUID],
    objective_id_map: dict[str, uuid.UUID],
    workshop_name_to_id: dict[str, uuid.UUID],
    s3_client,
    dry_run: bool,
) -> None:
    inserted = skipped = obj_links = workshop_links = images_uploaded = 0

    for rec in records:
        airtable_id = rec["id"]
        fields = rec.get("fields", {})

        name = fields.get("Asset Name") or fields.get("Resource Name") or ""
        if not name:
            print(f"  [skip] content_asset {airtable_id}: no name")
            skipped += 1
            continue

        description = fields.get("Description") or None
        content = fields.get("Content") or None
        link = fields.get("Link") or None
        embed_code = fields.get("Embed Code") or None
        is_featured = str(fields.get("Feature", "")).lower() in ("true", "1", "checked", "yes")
        for_counselor = str(fields.get("Counselor", "true")).lower() in ("true", "1", "checked", "yes")
        for_family = str(fields.get("Family", "true")).lower() in ("true", "1", "checked", "yes")

        # Resolve asset_type FK from airtable rec ID
        asset_type_airtable_id = fields.get("Asset Type")
        if isinstance(asset_type_airtable_id, list):
            asset_type_airtable_id = asset_type_airtable_id[0] if asset_type_airtable_id else None
        asset_type_db_id = asset_type_id_map.get(asset_type_airtable_id) if asset_type_airtable_id else None

        # Upload image to S3
        image_url: str | None = None
        img_attachment = _first_attachment(fields.get("Image"))
        if img_attachment:
            ext = _ext_from_content_type(img_attachment.get("type", ""))
            s3_key = f"assets/content/{airtable_id}/image.{ext}"
            image_url = _upload_attachment(s3_client, img_attachment, s3_key, dry_run)
            if image_url:
                images_uploaded += 1

        # Upload file to S3
        file_url: str | None = None
        file_attachment = _first_attachment(fields.get("File"))
        if file_attachment:
            filename = file_attachment.get("filename", "file.bin")
            s3_key = f"assets/content/{airtable_id}/{filename}"
            file_url = _upload_attachment(s3_client, file_attachment, s3_key, dry_run)

        row_id = uuid.uuid4()

        if not dry_run:
            conn.execute(
                text(
                    """
                    INSERT INTO content_assets (
                        id, airtable_id, asset_type_id, name, description,
                        content, link, embed_code, image_url, file_url, is_featured,
                        for_counselor, for_family
                    )
                    VALUES (
                        :id, :airtable_id, :asset_type_id, :name, :description,
                        :content, :link, :embed_code, :image_url, :file_url, :is_featured,
                        :for_counselor, :for_family
                    )
                    ON CONFLICT (airtable_id) DO UPDATE
                        SET asset_type_id  = EXCLUDED.asset_type_id,
                            name           = EXCLUDED.name,
                            description    = EXCLUDED.description,
                            content        = EXCLUDED.content,
                            link           = EXCLUDED.link,
                            embed_code     = EXCLUDED.embed_code,
                            image_url      = COALESCE(EXCLUDED.image_url, content_assets.image_url),
                            file_url       = COALESCE(EXCLUDED.file_url, content_assets.file_url),
                            is_featured    = EXCLUDED.is_featured,
                            for_counselor  = EXCLUDED.for_counselor,
                            for_family     = EXCLUDED.for_family
                    """
                ),
                {
                    "id": str(row_id),
                    "airtable_id": airtable_id,
                    "asset_type_id": str(asset_type_db_id) if asset_type_db_id else None,
                    "name": name,
                    "description": description,
                    "content": content,
                    "link": link,
                    "embed_code": embed_code,
                    "image_url": image_url,
                    "file_url": file_url,
                    "is_featured": is_featured,
                    "for_counselor": for_counselor,
                    "for_family": for_family,
                },
            )

            # Re-fetch the actual DB id (handles ON CONFLICT UPDATE case)
            result = conn.execute(
                text("SELECT id FROM content_assets WHERE airtable_id = :aid"),
                {"aid": airtable_id},
            ).fetchone()
            db_row_id = result[0] if result else str(row_id)

            # Link to objectives
            objective_airtable_ids = fields.get("Objective", "")
            if isinstance(objective_airtable_ids, list):
                obj_ids = objective_airtable_ids
            else:
                obj_ids = [x.strip() for x in str(objective_airtable_ids).split(",") if x.strip()]

            for oid in obj_ids:
                obj_db_id = objective_id_map.get(oid)
                if obj_db_id:
                    conn.execute(
                        text(
                            """
                            INSERT INTO content_asset_objectives (content_asset_id, objective_id)
                            VALUES (:cid, :oid)
                            ON CONFLICT DO NOTHING
                            """
                        ),
                        {"cid": str(db_row_id), "oid": str(obj_db_id)},
                    )
                    obj_links += 1

            # Link to workshops by name
            workshop_names_raw = fields.get("Name (from Workshops)", "")
            if isinstance(workshop_names_raw, list):
                wnames = workshop_names_raw
            else:
                wnames = [n.strip() for n in str(workshop_names_raw).split(",") if n.strip()]

            for wname in wnames:
                wid = workshop_name_to_id.get(wname)
                if wid:
                    conn.execute(
                        text(
                            """
                            INSERT INTO content_asset_workshops (content_asset_id, workshop_id)
                            VALUES (:cid, :wid)
                            ON CONFLICT DO NOTHING
                            """
                        ),
                        {"cid": str(db_row_id), "wid": str(wid)},
                    )
                    workshop_links += 1

        inserted += 1

    print(
        f"  content_assets: {inserted} upserted, {skipped} skipped, "
        f"{images_uploaded} images uploaded, "
        f"{obj_links} objective links, {workshop_links} workshop links"
    )


# ── Workshop name lookup ──────────────────────────────────────────────────────

def _load_workshop_name_map(conn) -> dict[str, uuid.UUID]:
    rows = conn.execute(text("SELECT id, name FROM workshops")).fetchall()
    return {row[1]: row[0] for row in rows}


# ── Reset helpers ─────────────────────────────────────────────────────────────

RESET_TABLES = [
    "content_asset_workshops",
    "content_asset_objectives",
    "content_assets",
    "objective_workshops",
    "objectives",
    "asset_types",
]


def _reset(conn) -> None:
    for t in RESET_TABLES:
        conn.execute(text(f"TRUNCATE TABLE {t} CASCADE"))
    print(f"  truncated: {', '.join(RESET_TABLES)}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import content assets from Airtable asset base into Postgres"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and process data without writing to DB or S3",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Truncate all content asset tables before importing",
    )
    parser.add_argument(
        "--table",
        choices=["asset_types", "objectives", "content_assets"],
        help="Import only a specific table (default: all in order)",
    )
    args = parser.parse_args()

    if not settings.airtable_api_key:
        print("Error: AIRTABLE_API_KEY is not set", file=sys.stderr)
        return 1
    if not settings.airtable_asset_base_id:
        print("Error: AIRTABLE_ASSET_BASE_ID is not set", file=sys.stderr)
        return 1
    if not settings.database_url:
        print("Error: DATABASE_URL is not set", file=sys.stderr)
        return 1

    api = Api(settings.airtable_api_key)
    base_id = settings.airtable_asset_base_id
    s3 = _s3()

    print("Fetching Airtable records...")
    asset_type_records = _fetch_table(api, base_id, "Asset Type")
    objective_records = _fetch_table(api, base_id, "Objectives")
    content_asset_records = _fetch_table(api, base_id, "Assets")
    print(
        f"  asset_types: {len(asset_type_records)}, "
        f"objectives: {len(objective_records)}, "
        f"content_assets: {len(content_asset_records)}"
    )

    with get_engine().begin() as conn:
        if args.reset and not args.dry_run:
            print("Resetting tables...")
            _reset(conn)

        workshop_name_map = _load_workshop_name_map(conn)
        print(f"  loaded {len(workshop_name_map)} workshops from DB")

        run_all = args.table is None

        asset_type_id_map: dict[str, uuid.UUID] = {}
        objective_id_map: dict[str, uuid.UUID] = {}

        if run_all or args.table == "asset_types":
            print("Importing asset_types...")
            asset_type_id_map = import_asset_types(conn, asset_type_records, s3, args.dry_run)

        if run_all or args.table == "objectives":
            # If asset_types were not imported this run, load existing IDs from DB
            if not asset_type_id_map and not args.dry_run:
                rows = conn.execute(
                    text("SELECT airtable_id, id FROM asset_types WHERE airtable_id IS NOT NULL")
                ).fetchall()
                asset_type_id_map = {r[0]: r[1] for r in rows}

            print("Importing objectives...")
            objective_id_map = import_objectives(
                conn, objective_records, workshop_name_map, args.dry_run
            )

        if run_all or args.table == "content_assets":
            # Load objective IDs from DB if not imported this run
            if not objective_id_map and not args.dry_run:
                rows = conn.execute(
                    text("SELECT airtable_id, id FROM objectives WHERE airtable_id IS NOT NULL")
                ).fetchall()
                objective_id_map = {r[0]: r[1] for r in rows}

            # Load asset_type IDs from DB if not imported this run
            if not asset_type_id_map and not args.dry_run:
                rows = conn.execute(
                    text("SELECT airtable_id, id FROM asset_types WHERE airtable_id IS NOT NULL")
                ).fetchall()
                asset_type_id_map = {r[0]: r[1] for r in rows}

            print("Importing content_assets...")
            import_content_assets(
                conn,
                content_asset_records,
                asset_type_id_map,
                objective_id_map,
                workshop_name_map,
                s3,
                args.dry_run,
            )

    if args.dry_run:
        print("\nDry run complete — no data was written.")
    else:
        print("\nImport complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
