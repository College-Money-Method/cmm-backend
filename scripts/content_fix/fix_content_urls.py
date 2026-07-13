"""
Migrate old marketing-site URLs in content bodies (TipTap JSON) on the DEV
database to the new site, per the CMM content-revision audit. Operates on
`topics` AND `content_assets`.

Two migrations:

1. WP file downloads -> S3 (deterministic, absolute; works in link marks AND
   rawHtml). Any
     https://[www.]collegemoneymethod.com/wp-content/uploads/YYYY/MM/<FILE>
   becomes
     https://cmm-general.s3.us-east-1.amazonaws.com/wordpress-media/<FILE>
   DOCUMENT extensions only (pdf/xlsx/doc/…). Images are EXCLUDED — they were
   not migrated to wordpress-media/ (verified 403), so rewriting would break them.

2. Marketing content pages -> in-app resource pages (link marks only). Each old
   page path maps to the canonical content_asset (the *published, no-external-link*
   native resource). Rendered content has no school-slug context and lives under
   /school/<slug>/... , so we use a RELATIVE link
     ../resources/<asset-id>
   which resolves to /school/<slug>/resources/<asset-id> from BOTH topic pages
   (/school/<slug>/topic/<slug>) and resource pages (/school/<slug>/resources/<id>),
   for ANY school. (Absolute /school/hampton-school/... would break other schools.)
   rawHtml occurrences are left untouched — links inside sandboxed iframes cannot
   navigate anyway (tracked separately).

Re-running on already-migrated rows is a no-op.
NOT auto-fixed (need manual input): href="#" in about-federal-loans (unknown
target); rawHtml-only .../resources/ listing link; .../income-limits-to-receive-
the-pell-grant/ (target asset is archived, no live resource); inline WP image
.../fafsa-logo-Short.png (not migrated to S3).

Dry-run by default. Pass --apply to commit.
Target DB: DEV DATABASE_URL from .env.dev (override: --database-url / env DATABASE_URL).

Usage:
    uv run python -m scripts.content_fix.fix_content_urls                     # dry-run, both tables
    uv run python -m scripts.content_fix.fix_content_urls --apply             # commit
    uv run python -m scripts.content_fix.fix_content_urls --table content_assets --apply
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.db.base import get_engine  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent.parent
S3_BASE = "https://cmm-general.s3.us-east-1.amazonaws.com/wordpress-media"

# WP upload path (any host/date) -> S3 wordpress-media/<basename>.
# DOCUMENT files only (images excluded: not migrated to wordpress-media/).
WP_UPLOAD_RE = re.compile(
    r"https?://(?:www\.)?collegemoneymethod\.com/wp-content/uploads/\d+/\d+/"
    r"([^\"'\s)]+?\.(?:pdf|xlsx|xls|docx|doc|pptx|ppt|csv|zip))",
    re.IGNORECASE,
)

# Marketing content-page path (leading/trailing slashes stripped, lowercased)
# -> canonical content_asset id (published, no external link).
PAGE_TO_ASSET = {
    "2026-27-fafsa-student-aid-index-sai-calculator": "2869e63a-c1a3-4fa0-9b77-d6c3df0ff8c7",
    "how-to-create-your-fsa-id": "d862bf45-4ac4-4a1f-aa2e-a134ec952286",
    "financial-aid-application-process-for-single-separated-or-divorced-parents": "6944eec6-f9ca-41b0-9e2e-db07d0ad04c5",
    "how-to-report-parent-investments-on-fafsa-css-profile": "28d355e0-96d5-4181-906f-a75760ec5212",
    "merit-based-aid-data": "e7f57b7e-e7a5-43ba-a783-61832e7a8a85",
    "merit-based-financial-aid-data": "e7f57b7e-e7a5-43ba-a783-61832e7a8a85",
    "need-based-financial-aid-data": "eab44ea4-3fed-4f12-b9c7-71f2663af3ea",
    # content_assets (resource-to-resource cross-refs)
    "creating-your-fsa-id": "d862bf45-4ac4-4a1f-aa2e-a134ec952286",
    "how-business-assets-are-used-in-financial-aid": "9aa74f97-9179-4641-ae02-2d5da56ddb22",
    "how-css-profile-colleges-count-home-equity": "5ac9ee9d-ceee-4511-a85a-f9212eb4c8c7",
}
CMM_HOST_RE = re.compile(r"^https?://(?:www\.)?collegemoneymethod\.com/(.*)$", re.IGNORECASE)

# table -> label column for per-row output.
TABLES = {"topics": "slug", "content_assets": "name"}


def resolve_database_url(cli_url: str | None) -> str:
    if cli_url:
        return cli_url
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    dev_url = dotenv_values(PROJECT_ROOT / ".env.dev").get("DATABASE_URL")
    if not dev_url:
        raise SystemExit("No DATABASE_URL found (checked --database-url, env, .env.dev).")
    return dev_url


def page_target(href: str) -> str | None:
    """Return relative resource link if href is a mapped marketing content page."""
    m = CMM_HOST_RE.match(href.strip())
    if not m:
        return None
    path = m.group(1).split("?")[0].split("#")[0].strip("/").lower()
    asset_id = PAGE_TO_ASSET.get(path)
    return f"../resources/{asset_id}" if asset_id else None


def rewrite_node(node: dict, tally: dict[str, int]) -> None:
    """In-place: fix link-mark hrefs (files + pages) and WP file URLs inside rawHtml."""
    if not isinstance(node, dict):
        return

    # 1 + 2: link marks
    for mark in node.get("marks") or []:
        if isinstance(mark, dict) and mark.get("type") == "link":
            attrs = mark.get("attrs") or {}
            href = attrs.get("href") or ""
            new_file = WP_UPLOAD_RE.sub(lambda mm: f"{S3_BASE}/{mm.group(1)}", href)
            if new_file != href:
                attrs["href"] = new_file
                tally["file_linkmark"] += 1
                continue
            target = page_target(href)
            if target:
                attrs["href"] = target
                tally["page_linkmark"] += 1

    # 1 (rawHtml only): WP file URLs -> S3
    if node.get("type") == "rawHtml":
        attrs = node.get("attrs") or {}
        html = attrs.get("html") or ""
        new_html, n = WP_UPLOAD_RE.subn(lambda mm: f"{S3_BASE}/{mm.group(1)}", html)
        if n:
            attrs["html"] = new_html
            tally["file_rawhtml"] += n

    for child in node.get("content") or []:
        rewrite_node(child, tally)


def process_table(conn, table: str, label_col: str, apply: bool, grand: dict[str, int]) -> int:
    """Rewrite URLs in one table. Returns rows changed; accumulates into `grand`."""
    rows = conn.execute(text(
        f"SELECT id, {label_col} AS label, content FROM {table} "
        f"WHERE content IS NOT NULL ORDER BY {label_col}"
    )).mappings().all()

    changed = 0
    for row in rows:
        try:
            doc = json.loads(row["content"])
        except json.JSONDecodeError:
            continue
        tally = {"file_linkmark": 0, "file_rawhtml": 0, "page_linkmark": 0}
        rewrite_node(doc, tally)
        if not any(tally.values()):
            continue
        changed += 1
        for k, v in tally.items():
            grand[k] += v
        parts = [f"{k}={v}" for k, v in tally.items() if v]
        print(f"  [{table}] {row['label']}: {', '.join(parts)}")
        if apply:
            conn.execute(
                text(f"UPDATE {table} SET content = :c, updated_at = now() WHERE id = :id"),
                {"c": json.dumps(doc, ensure_ascii=False), "id": row["id"]},
            )
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate old URLs in dev content bodies.")
    parser.add_argument("--database-url", dest="database_url", default=None)
    parser.add_argument("--table", choices=[*TABLES, "both"], default="both")
    parser.add_argument("--apply", action="store_true", help="Commit changes (default: dry-run).")
    args = parser.parse_args()

    db_url = resolve_database_url(args.database_url)
    print(f"Connecting to: {re.sub(r'//[^@]+@', '//***@', db_url)}")
    print(f"Mode: {'APPLY (will commit)' if args.apply else 'DRY-RUN (no writes)'}\n")

    tables = list(TABLES) if args.table == "both" else [args.table]
    engine = get_engine(db_url)
    grand = {"file_linkmark": 0, "file_rawhtml": 0, "page_linkmark": 0}
    total_changed = 0

    with engine.begin() as conn:
        for table in tables:
            total_changed += process_table(conn, table, TABLES[table], args.apply, grand)
        if not args.apply:
            conn.rollback()

    print(f"\nRows affected: {total_changed}")
    print(f"  WP->S3 file links (link marks): {grand['file_linkmark']}")
    print(f"  WP->S3 file links (rawHtml):    {grand['file_rawhtml']}")
    print(f"  Marketing page -> resource:     {grand['page_linkmark']}")
    if not args.apply:
        print("\nDry-run only. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
