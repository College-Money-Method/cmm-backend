#!/usr/bin/env python3
"""
Ingest CMM Resource Center asset data from CSV into Postgres.

Reads:  scripts/input/resource_ingest/CMM_Resource_Center_Assets_Preview content_v1.xlsx - CMM Resource Center Assets.csv

Actions per row:
  - Upserts asset_types by name
  - Upserts content_assets (conflict on airtable_id = R1, R2, …)
  - Replaces topic_resources links
  - Replaces workshop_resources links

Usage (from project root):
  uv run python scripts/ingest_resource_csv.py
  uv run python scripts/ingest_resource_csv.py --dry-run
  uv run python scripts/ingest_resource_csv.py --reset
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

from src.db.base import get_engine

# ── CSV location ──────────────────────────────────────────────────────────────

CSV_PATH = (
    Path(__file__).parent
    / "input"
    / "resource_ingest"
    / "CMM_Resource_Center_Assets_Preview content_v1.xlsx - CMM Resource Center Assets.csv"
)


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_status(notes: str) -> str:
    """Return 'archived' for ARCHIVE/DUPLICATE flags, 'draft' for missing link, else 'published'."""
    n = notes.upper()
    if "ARCHIVE OLDER YEAR" in n or "DUPLICATE" in n:
        return "archived"
    return "published"


def parse_time_estimate(time_estimate: str) -> int | None:
    """Convert '15–20 min' → 17, '5 min' → 5, '<5 min' → 5, empty → None."""
    if not time_estimate:
        return None
    nums = re.findall(r"\d+", time_estimate)
    if not nums:
        return None
    values = [int(n) for n in nums]
    return round(sum(values) / len(values))


def parse_grades(grades_str: str) -> str | None:
    """
    '9th–12th' → '9,10,11,12'
    '12th'     → '12'
    '9th–10th' → '9,10'
    """
    if not grades_str.strip():
        return None
    nums = [int(n) for n in re.findall(r"\d+", grades_str)]
    if not nums:
        return None
    if len(nums) == 1:
        return str(nums[0])
    # Range: expand from first to last
    return ",".join(str(g) for g in range(nums[0], nums[-1] + 1))


def parse_tags(tags_str: str) -> list[str]:
    """'Tag1, Tag2, Tag3' → ['Tag1', 'Tag2', 'Tag3']."""
    if not tags_str.strip():
        return []
    return [t.strip() for t in tags_str.split(",") if t.strip()]


def parse_is_featured(notes: str) -> bool:
    return "featured resource" in notes.lower()


def parse_primary_asset_type(asset_type_str: str) -> str:
    """'Calculator, Guide' → 'Calculator' (use first type)."""
    return asset_type_str.split(",")[0].strip()


def parse_topic_titles(topics_str: str) -> list[str]:
    """
    'T3: How Need-Based Aid Is Awarded, T10: Calculating Parent Income'
    → ['How Need-Based Aid Is Awarded', 'Calculating Parent Income']
    """
    if not topics_str.strip():
        return []
    titles = []
    for part in topics_str.split(","):
        part = part.strip()
        # Match "T\d+: Title"
        m = re.match(r"T\d+:\s*(.+)", part)
        if m:
            titles.append(m.group(1).strip())
        elif part:
            titles.append(part)  # fallback: use as-is
    return titles


def parse_workshop_numbers(workshops_str: str) -> list[int]:
    """'Workshop #1, Workshop #2' → [1, 2] (sequence_number values)."""
    return [int(m) for m in re.findall(r"#(\d+)", workshops_str)]


# ── DB lookups ────────────────────────────────────────────────────────────────

def load_topic_map(conn) -> list[tuple[str, str]]:
    """Returns list of (title_lower, uuid_str) for fuzzy matching."""
    rows = conn.execute(text("SELECT id, title FROM topics")).fetchall()
    return [(row[1].lower(), str(row[0])) for row in rows]


def _normalize_title(s: str) -> str:
    """Normalize for fuzzy matching: lowercase, remove all articles, replace & with 'and'."""
    s = s.lower().strip()
    s = s.replace(" & ", " and ").replace("&", "and")
    # Remove articles anywhere so "Completing the Income Sections" → "completing income sections"
    s = re.sub(r"\b(the|a|an)\b\s*", "", s)
    # Collapse multiple spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


def find_topic_id(csv_title: str, topic_entries: list[tuple[str, str]]) -> str | None:
    """
    Fuzzy-match a CSV topic title against DB topic titles.
    Strategies (in order): exact → normalized exact → starts-with → contained-in.
    Returns the UUID of the first match, or None.
    """
    needle = csv_title.lower().strip()
    needle_norm = _normalize_title(csv_title)

    for title, uid in topic_entries:
        if title == needle:
            return uid

    norm_entries = [(_normalize_title(title), uid) for title, uid in topic_entries]

    # Normalized exact
    for title_norm, uid in norm_entries:
        if title_norm == needle_norm:
            return uid

    # Normalized starts-with (either direction)
    for title_norm, uid in norm_entries:
        if title_norm.startswith(needle_norm) or needle_norm.startswith(title_norm):
            return uid

    # Normalized contains
    for title_norm, uid in norm_entries:
        if needle_norm in title_norm:
            return uid

    # All content words from needle appear in DB title (handles "Research Merit Opportunities" vs "Research School Merit Opportunities")
    needle_words = {w for w in needle_norm.split() if len(w) > 2}
    if needle_words:
        for title_norm, uid in norm_entries:
            db_words = set(title_norm.split())
            if needle_words <= db_words:
                return uid

    return None


def load_workshop_map(conn) -> dict[int, str]:
    """Workshop sequence_number → workshop UUID string (only for numbered workshops)."""
    rows = conn.execute(
        text("SELECT id, sequence_number FROM workshops WHERE sequence_number IS NOT NULL")
    ).fetchall()
    return {row[1]: str(row[0]) for row in rows}


def load_asset_type_map(conn) -> dict[str, str]:
    """Asset type name → UUID string."""
    rows = conn.execute(text("SELECT id, name FROM asset_types")).fetchall()
    return {row[1]: str(row[0]) for row in rows}


# ── Upsert helpers ────────────────────────────────────────────────────────────

def upsert_asset_type(conn, name: str, asset_type_map: dict[str, str], dry_run: bool) -> str:
    """Ensure asset type exists; return its UUID string."""
    if name in asset_type_map:
        return asset_type_map[name]
    new_id = str(uuid.uuid4())
    if not dry_run:
        conn.execute(
            text(
                """
                INSERT INTO asset_types (id, name)
                VALUES (:id, :name)
                ON CONFLICT (name) DO NOTHING
                """
            ),
            {"id": new_id, "name": name},
        )
        # Re-fetch in case of conflict
        row = conn.execute(
            text("SELECT id FROM asset_types WHERE name = :name"), {"name": name}
        ).fetchone()
        actual_id = str(row[0]) if row else new_id
    else:
        actual_id = new_id
    asset_type_map[name] = actual_id
    return actual_id


def upsert_content_asset(conn, row: dict, asset_type_id: str | None, dry_run: bool) -> str | None:
    """Insert or update content_asset row; returns DB UUID string."""
    airtable_id = row["ID"].strip()

    name = row["Edited Resource Name"].strip() or row["Asset Name"].strip() or row.get("Resource Name", "").strip()
    if not name:
        print(f"  [skip] {airtable_id}: no name")
        return None

    link = row["Link"].strip() or None
    notes = row["Notes"].strip()

    status = "draft" if not link else parse_status(notes)
    is_featured = parse_is_featured(notes)
    time_estimate = parse_time_estimate(row["Time Estimate"].strip())
    description = row["Edited Description"].strip() or row["Description"].strip() or None
    why_important = row["Why Is This Important?"].strip() or None
    how_to_use = row["How to Use This"].strip() or None
    suggested_grades = parse_grades(row["Grades"].strip())

    row_id = str(uuid.uuid4())

    if dry_run:
        tags = parse_tags(row.get("Tags", ""))
        print(
            f"  [dry-run] {airtable_id}: {name!r} | status={status} "
            f"is_featured={is_featured} time_estimate={time_estimate} "
            f"grades={suggested_grades} tags={tags}"
        )
        return row_id

    conn.execute(
        text(
            """
            INSERT INTO content_assets (
                id, airtable_id, asset_type_id, name, description,
                link, is_featured, status,
                why_important, how_to_use, suggested_grades,
                time_estimate_minutes
            )
            VALUES (
                :id, :airtable_id, :asset_type_id, :name, :description,
                :link, :is_featured, :status,
                :why_important, :how_to_use, :suggested_grades,
                :time_estimate_minutes
            )
            ON CONFLICT (airtable_id) DO UPDATE
                SET asset_type_id        = EXCLUDED.asset_type_id,
                    name                 = EXCLUDED.name,
                    description          = EXCLUDED.description,
                    link                 = EXCLUDED.link,
                    is_featured          = EXCLUDED.is_featured,
                    status               = EXCLUDED.status,
                    why_important        = EXCLUDED.why_important,
                    how_to_use           = EXCLUDED.how_to_use,
                    suggested_grades     = EXCLUDED.suggested_grades,
                    time_estimate_minutes = EXCLUDED.time_estimate_minutes,
                    updated_at           = now()
            """
        ),
        {
            "id": row_id,
            "airtable_id": airtable_id,
            "asset_type_id": asset_type_id,
            "name": name,
            "description": description,
            "link": link,
            "is_featured": is_featured,
            "status": status,
            "why_important": why_important,
            "how_to_use": how_to_use,
            "suggested_grades": suggested_grades,
            "time_estimate_minutes": time_estimate,
        },
    )

    # Fetch the real DB id (handles ON CONFLICT case)
    result = conn.execute(
        text("SELECT id FROM content_assets WHERE airtable_id = :aid"),
        {"aid": airtable_id},
    ).fetchone()
    return str(result[0]) if result else row_id


def link_topics(
    conn,
    db_asset_id: str,
    airtable_id: str,
    topic_titles: list[str],
    topic_entries: list[tuple[str, str]],
    dry_run: bool,
) -> tuple[int, list[str]]:
    """Replace topic_resources for this asset. Returns (linked_count, unmatched_titles)."""
    if not dry_run:
        conn.execute(
            text("DELETE FROM topic_resources WHERE content_asset_id = :aid"),
            {"aid": db_asset_id},
        )

    linked = 0
    unmatched = []
    seen_topic_ids: set[str] = set()
    for i, title in enumerate(topic_titles):
        topic_id = find_topic_id(title, topic_entries)
        if not topic_id:
            unmatched.append(title)
            continue
        if topic_id in seen_topic_ids:
            continue  # skip duplicate match (two CSV titles resolving to same topic)
        seen_topic_ids.add(topic_id)
        if not dry_run:
            conn.execute(
                text(
                    """
                    INSERT INTO topic_resources (topic_id, content_asset_id, sort_order)
                    VALUES (:tid, :aid, :sort)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"tid": topic_id, "aid": db_asset_id, "sort": i},
            )
        linked += 1

    return linked, unmatched


def link_workshops(
    conn,
    db_asset_id: str,
    airtable_id: str,
    workshop_numbers: list[int],
    workshop_map: dict[int, str],
    dry_run: bool,
) -> tuple[int, list[int]]:
    """Replace workshop_resources for this asset. Returns (linked_count, unmatched_numbers)."""
    if not dry_run:
        conn.execute(
            text("DELETE FROM workshop_resources WHERE content_asset_id = :aid"),
            {"aid": db_asset_id},
        )

    linked = 0
    unmatched = []
    for i, num in enumerate(workshop_numbers):
        workshop_id = workshop_map.get(num)
        if not workshop_id:
            unmatched.append(num)
            continue
        if not dry_run:
            conn.execute(
                text(
                    """
                    INSERT INTO workshop_resources (content_asset_id, workshop_id, sort_order)
                    VALUES (:aid, :wid, :sort)
                    ON CONFLICT DO NOTHING
                    """
                ),
                {"aid": db_asset_id, "wid": workshop_id, "sort": i},
            )
        linked += 1

    return linked, unmatched


def upsert_tag(conn, name: str) -> str:
    """Upsert a tag by name into `tags` table; return its UUID string."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    tag_id = str(uuid.uuid4())
    conn.execute(
        text(
            """
            INSERT INTO tags (id, name, slug)
            VALUES (:id, :name, :slug)
            ON CONFLICT (name) DO NOTHING
            """
        ),
        {"id": tag_id, "name": name, "slug": slug},
    )
    row = conn.execute(
        text("SELECT id FROM tags WHERE name = :name"), {"name": name}
    ).fetchone()
    return str(row[0]) if row else tag_id


def link_tags(conn, db_asset_id: str, tag_names: list[str], dry_run: bool) -> int:
    """Replace content_asset_tags for this asset. Returns linked count."""
    if dry_run:
        return 0
    conn.execute(
        text("DELETE FROM content_asset_tags WHERE content_asset_id = :aid"),
        {"aid": db_asset_id},
    )
    if not tag_names:
        return 0
    linked = 0
    for name in tag_names:
        tag_id = upsert_tag(conn, name)
        conn.execute(
            text(
                """
                INSERT INTO content_asset_tags (content_asset_id, tag_id)
                VALUES (:aid, :tid)
                ON CONFLICT DO NOTHING
                """
            ),
            {"aid": db_asset_id, "tid": tag_id},
        )
        linked += 1
    return linked


# ── Reset ─────────────────────────────────────────────────────────────────────

def reset_csv_assets(conn) -> None:
    """Remove all content_assets rows ingested from this CSV (airtable_id LIKE 'R%' and numeric)."""
    result = conn.execute(
        text(
            """
            DELETE FROM content_assets
            WHERE airtable_id ~ '^R[0-9]+$'
            """
        )
    )
    print(f"  reset: deleted {result.rowcount} content_asset rows with airtable_id R[0-9]+")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest Resource Center CSV into Postgres")
    parser.add_argument("--dry-run", action="store_true", help="No DB writes")
    parser.add_argument("--reset", action="store_true", help="Delete R* assets before ingest")
    parser.add_argument("--csv", default=str(CSV_PATH), help="Path to CSV file")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Error: CSV not found at {csv_path}", file=sys.stderr)
        return 1

    print(f"Reading CSV: {csv_path.name}")
    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print(f"  {len(rows)} rows found")

    with get_engine().begin() as conn:
        if args.reset and not args.dry_run:
            print("Resetting R* assets...")
            reset_csv_assets(conn)

        topic_entries = load_topic_map(conn)
        workshop_map = load_workshop_map(conn)
        asset_type_map = load_asset_type_map(conn)
        print(f"  DB lookups: {len(topic_entries)} topics, {len(workshop_map)} workshops, {len(asset_type_map)} asset_types")

        inserted = skipped = topic_links = workshop_links = tag_links = 0
        all_unmatched_topics: list[str] = []
        all_unmatched_workshops: list[str] = []

        for row in rows:
            airtable_id = row.get("ID", "").strip()
            if not airtable_id:
                continue

            # Resolve asset type (use first if combined like "Calculator, Guide")
            raw_type = row.get("Asset Type", "").strip()
            primary_type = parse_primary_asset_type(raw_type)
            asset_type_id: str | None = None
            if primary_type:
                asset_type_id = upsert_asset_type(conn, primary_type, asset_type_map, args.dry_run)

            db_id = upsert_content_asset(conn, row, asset_type_id, args.dry_run)
            if db_id is None:
                skipped += 1
                continue
            inserted += 1

            if not args.dry_run:
                # Topic links
                topic_titles = parse_topic_titles(row.get("Topics", ""))
                tlinked, tunmatched = link_topics(conn, db_id, airtable_id, topic_titles, topic_entries, args.dry_run)
                topic_links += tlinked
                if tunmatched:
                    all_unmatched_topics.extend(f"{airtable_id} → {t!r}" for t in tunmatched)

                # Workshop links (match by sequence_number)
                wnums = parse_workshop_numbers(row.get("Workshops", ""))
                wlinked, wunmatched = link_workshops(conn, db_id, airtable_id, wnums, workshop_map, args.dry_run)
                workshop_links += wlinked
                if wunmatched:
                    all_unmatched_workshops.extend(f"{airtable_id} → Workshop #{w}" for w in wunmatched)

                # Tag links
                tag_names = parse_tags(row.get("Tags", ""))
                tag_links += link_tags(conn, db_id, tag_names, args.dry_run)

        print(f"\ncontent_assets: {inserted} upserted, {skipped} skipped")
        print(f"topic_resources: {topic_links} links")
        print(f"workshop_resources: {workshop_links} links")
        print(f"content_asset_tags: {tag_links} links")

        if all_unmatched_topics:
            print(f"\n[warn] {len(all_unmatched_topics)} unmatched topic title(s):")
            for m in all_unmatched_topics:
                print(f"  {m}")

        if all_unmatched_workshops:
            print(f"\n[warn] {len(all_unmatched_workshops)} unmatched workshop name(s):")
            for m in all_unmatched_workshops:
                print(f"  {m}")

    if args.dry_run:
        print("\nDry run complete — no data written.")
    else:
        print("\nIngest complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
