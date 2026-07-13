#!/usr/bin/env python3
"""
Sync content from WordPress REST API into content_assets.

For each content_asset whose link points to the configured WP domain
(skipping wp-content file upload URLs):
  - Extracts the URL slug
  - Fetches title, content.rendered, and excerpt.rendered via WP REST API
  - Sanitizes the HTML (strips <script>, on* event attrs)
  - Updates name (title), content, and description in content_assets
  - Records wp_post_id and wp_synced_at for audit trail

Usage (from project root):
  uv run python scripts/migrate_wp_content.py --wp-domain https://yoursite.com
  uv run python scripts/migrate_wp_content.py --wp-domain https://yoursite.com --dry-run
  uv run python scripts/migrate_wp_content.py --wp-domain https://yoursite.com --overwrite
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text

from src.config import settings
from src.db.base import get_engine


# ── HTML sanitization ─────────────────────────────────────────────────────────

_UNSAFE_TAGS = re.compile(
    r"<(script|style|object|embed|form|input|button)[^>]*>.*?</\1>",
    re.IGNORECASE | re.DOTALL,
)
_SELF_CLOSING_UNSAFE = re.compile(
    r"<(script|style|input|form)[^>]*/?>",
    re.IGNORECASE,)
_EVENT_ATTRS = re.compile(
    r'\s+on\w+\s*=\s*(?:"[^"]*"|\'[^\']*\'|[^\s>]*)',
    re.IGNORECASE,
)
_JAVASCRIPT_HREF = re.compile(
    r'(href|src)\s*=\s*["\']javascript:[^"\']*["\']',
    re.IGNORECASE,
)


def sanitize_html(html: str) -> str:
    """Strip unsafe tags and event attributes from HTML."""
    html = _UNSAFE_TAGS.sub("", html)
    html = _SELF_CLOSING_UNSAFE.sub("", html)
    html = _EVENT_ATTRS.sub("", html)
    html = _JAVASCRIPT_HREF.sub('href="#"', html)
    return html.strip()


# ── WP REST API ───────────────────────────────────────────────────────────────

def fetch_wp_post(wp_domain: str, slug: str) -> dict | None:
    """Fetch a WordPress post by slug. Returns the post dict or None."""
    url = f"{wp_domain.rstrip('/')}/wp-json/wp/v2/posts"
    try:
        resp = requests.get(
            url,
            params={"slug": slug, "_fields": "id,title,content,excerpt,link"},
            timeout=15,
        )
        resp.raise_for_status()
        posts = resp.json()
        return posts[0] if posts else None
    except Exception as e:
        print(f"    [warn] WP API error for slug '{slug}': {e}")
        return None


def is_wp_content_link(link: str) -> bool:
    """Return True if the link points to a wp-content file upload (not a post)."""
    return "/wp-content/" in link


def extract_slug(link: str, wp_domain: str) -> str | None:
    """Extract the last path segment (slug) from a WP URL."""
    try:
        parsed = urlparse(link)
        wp_parsed = urlparse(wp_domain)
        if parsed.netloc != wp_parsed.netloc:
            return None
        # Path: /some/path/slug/ → "slug"
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        return parts[-1] if parts else None
    except Exception:
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync WordPress title/content/description into content_assets"
    )
    parser.add_argument(
        "--wp-domain",
        required=True,
        help="WordPress site base URL, e.g. https://yoursite.com",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be migrated without writing to DB",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite content even if content is already populated",
    )
    args = parser.parse_args()

    engine = get_engine()
    migrated = skipped = failed = no_post = 0

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, name, link, content, description
                FROM content_assets
                WHERE link IS NOT NULL AND link != ''
                ORDER BY created_at
                """
            )
        ).fetchall()

        print(f"Found {len(rows)} assets with links")

        for row in rows:
            asset_id, name, link, existing_content, existing_desc = row

            # Skip file uploads — these aren't WordPress posts
            if is_wp_content_link(link):
                print(f"  [skip] '{name}' — wp-content file upload, not a post")
                skipped += 1
                continue

            slug = extract_slug(link, args.wp_domain)
            if not slug:
                skipped += 1
                continue

            if existing_content and not args.overwrite:
                print(f"  [skip] '{name}' — content already exists (use --overwrite)")
                skipped += 1
                continue

            print(f"  Fetching '{name}' (slug: {slug}) ...", end=" ")
            post = fetch_wp_post(args.wp_domain, slug)

            if not post:
                print("not found")
                no_post += 1
                continue

            raw_html = post.get("content", {}).get("rendered", "")
            wp_title = post.get("title", {}).get("rendered", "").strip()
            excerpt_text = re.sub(r"<[^>]+>", "", post.get("excerpt", {}).get("rendered", "")).strip()
            wp_post_id = str(post.get("id", ""))

            if not raw_html:
                print("empty content")
                no_post += 1
                continue

            clean_html = sanitize_html(raw_html)

            if args.dry_run:
                print(f"would write {len(clean_html)} chars, title='{wp_title}' (wp_id={wp_post_id})")
                migrated += 1
                continue

            conn.execute(
                text(
                    """
                    UPDATE content_assets SET
                        name         = :name,
                        content      = :content,
                        description  = :description,
                        wp_post_id   = :wp_post_id,
                        wp_synced_at = :wp_synced_at
                    WHERE id = :id
                    """
                ),
                {
                    "id": str(asset_id),
                    "name": wp_title or name,
                    "content": clean_html,
                    "description": excerpt_text or existing_desc or None,
                    "wp_post_id": wp_post_id,
                    "wp_synced_at": datetime.now(timezone.utc),
                },
            )
            print(f"✓ {len(clean_html)} chars, title='{wp_title}'")
            migrated += 1

    print()
    print(f"Done: {migrated} migrated, {skipped} skipped, {no_post} not found in WP, {failed} errors")
    if args.dry_run:
        print("(dry run — no data written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
