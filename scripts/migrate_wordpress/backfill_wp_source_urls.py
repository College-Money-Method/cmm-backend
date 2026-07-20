#!/usr/bin/env python3
"""
Backfill content_assets.wp_source_url — the original collegemoneymethod.com
POST/PAGE URL each asset was migrated from, so those pages can be crawled as
HTML later.

Only crawlable WP post/page URLs qualify. wp-content file-download URLs
(PDF/xlsx uploads) are NOT source pages — they're just the old `link` value —
and external URLs (studentaid.gov, etc.) never had a WP page. Both stay NULL.

Per row, resolution priority:

  1. link       — `link` is an old-site PAGE URL (not /wp-content/) → copy as-is
  2. wp_post_id — fetch post/page by stored ID via WP REST API → use its `link`
  3. slug       — titles were not changed during migration, so slugify(name) and
                  look the post/page up by slug (backup match)

Requires the WordPress site to still be online for steps 2–3 — run before
decommission. Rows with wp_source_url already set are skipped unless
--overwrite. Also self-heals: clears any wp_source_url mistakenly holding a
wp-content file URL (e.g. from an earlier run) before resolving.

Usage (from project root):
  uv run python scripts/migrate_wordpress/backfill_wp_source_urls.py --dry-run
  uv run python scripts/migrate_wordpress/backfill_wp_source_urls.py
  uv run python scripts/migrate_wordpress/backfill_wp_source_urls.py --overwrite
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from html import unescape
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text

from src.db.base import get_engine

WP_DOMAIN = "https://collegemoneymethod.com"
CMM_URL_RE = re.compile(r"^https?://(www\.)?collegemoneymethod\.com/", re.IGNORECASE)
REQUEST_DELAY_S = 0.2  # be polite to the WP server between API calls


def is_cmm_page_url(url: str | None) -> bool:
    """True if url is an old-site post/page URL (not a wp-content file upload)."""
    return bool(url) and bool(CMM_URL_RE.match(url)) and "/wp-content/" not in url


# ── WP REST API lookups ───────────────────────────────────────────────────────

def _wp_get(url: str, params: dict) -> list | dict | None:
    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"    [warn] WP API error ({url}): {e}")
        return None


def fetch_link_by_post_id(wp_domain: str, wp_post_id: str) -> str | None:
    """Resolve a stored wp_post_id to its canonical page URL. Tries posts then pages."""
    for endpoint in ("posts", "pages"):
        data = _wp_get(
            f"{wp_domain.rstrip('/')}/wp-json/wp/v2/{endpoint}/{wp_post_id}",
            params={"_fields": "link"},
        )
        if isinstance(data, dict) and data.get("link"):
            return data["link"]
    return None


def fetch_link_by_slug(wp_domain: str, slug: str) -> str | None:
    """Resolve a slug to its canonical page URL. Tries posts then pages."""
    for endpoint in ("posts", "pages"):
        data = _wp_get(
            f"{wp_domain.rstrip('/')}/wp-json/wp/v2/{endpoint}",
            params={"slug": slug, "_fields": "link"},
        )
        if isinstance(data, list) and data and data[0].get("link"):
            return data[0]["link"]
    return None


# ── Slug derivation (backup match) ────────────────────────────────────────────

def slugify(title: str) -> str:
    """Approximate WordPress's sanitize_title(): lowercase, punctuation stripped,
    whitespace → hyphens. Titles were not changed during migration, so this
    usually reproduces the original post slug."""
    slug = unescape(title).lower()
    slug = slug.replace("&", " and ")
    slug = re.sub(r"['‘’“”\"]", "", slug)  # drop quotes/apostrophes
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill content_assets.wp_source_url")
    parser.add_argument("--wp-domain", default=WP_DOMAIN, help=f"WP base URL (default: {WP_DOMAIN})")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--overwrite", action="store_true", help="Re-resolve rows that already have wp_source_url")
    args = parser.parse_args()

    engine = get_engine()

    # Self-heal: wp_source_url must never hold a file-download URL
    with engine.begin() as conn:
        bad = conn.execute(
            text("SELECT COUNT(*) FROM content_assets WHERE wp_source_url ILIKE '%/wp-content/%'")
        ).scalar()
        if bad:
            print(f"Clearing {bad} wp_source_url value(s) that hold wp-content file URLs")
            if not args.dry_run:
                conn.execute(
                    text(
                        "UPDATE content_assets SET wp_source_url = NULL "
                        "WHERE wp_source_url ILIKE '%/wp-content/%'"
                    )
                )

    where = "" if args.overwrite else "WHERE wp_source_url IS NULL"
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT id::text, name, link, wp_post_id
                FROM content_assets
                {where}
                ORDER BY created_at
                """
            )
        ).fetchall()

    print(f"Found {len(rows)} asset(s) to resolve\n")
    resolved: dict[str, tuple[str, str]] = {}  # id → (url, method)
    no_wp_page: list[str] = []                 # external/file assets with no WP page
    unresolved: list[tuple[str, str]] = []     # (id, name) — expected a page, found none

    for asset_id, name, link, wp_post_id in rows:
        url = method = None

        # 1. link is still an old-site page URL
        if is_cmm_page_url(link):
            url, method = link, "link"

        # 2. stored WP post ID
        if not url and wp_post_id:
            url = fetch_link_by_post_id(args.wp_domain, wp_post_id)
            method = "wp_post_id"
            time.sleep(REQUEST_DELAY_S)

        # 3. slug derived from the (unchanged) title
        if not url and name:
            slug = slugify(name)
            if slug:
                url = fetch_link_by_slug(args.wp_domain, slug)
                method = f"slug:{slug}"
                time.sleep(REQUEST_DELAY_S)

        if url:
            resolved[asset_id] = (url, method)
            print(f"  ✓ [{method}] {name}\n      → {url}")
        elif link and not CMM_URL_RE.match(link):
            # External resource (or S3-migrated file) that never had a WP page
            no_wp_page.append(name)
            print(f"  - no WP page (external/file asset): {name}")
        else:
            unresolved.append((asset_id, name))
            print(f"  ✗ unresolved: {name} (link={link!r}, wp_post_id={wp_post_id!r})")

    if resolved and not args.dry_run:
        with engine.begin() as conn:
            for asset_id, (url, _) in resolved.items():
                conn.execute(
                    text("UPDATE content_assets SET wp_source_url = :url WHERE id = :id"),
                    {"url": url, "id": asset_id},
                )

    print(
        f"\nDone: {len(resolved)} resolved, {len(no_wp_page)} without a WP page (left NULL), "
        f"{len(unresolved)} unresolved"
    )
    if unresolved:
        print("Unresolved assets (need manual lookup):")
        for asset_id, name in unresolved:
            print(f"  - {name} ({asset_id})")
    if args.dry_run:
        print("(dry run — nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
