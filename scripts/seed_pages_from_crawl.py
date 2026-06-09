#!/usr/bin/env python3
"""
Seed CMS pages by fetching HTML directly from collegemoneymethod.com.

Extracts the WordPress `.entry-content` area, preserving all links, tables,
and formatting. Stores clean HTML in the pages table.

WordPress assets (images, PDFs, etc. from /wp-content/uploads) are uploaded
to S3 and links are rewritten to S3 URLs.

Usage (from project root):
  uv run --extra scripts python scripts/seed_pages_from_crawl.py
  uv run --extra scripts python scripts/seed_pages_from_crawl.py --dry-run
  uv run --extra scripts python scripts/seed_pages_from_crawl.py --force
  uv run --extra scripts python scripts/seed_pages_from_crawl.py --env dev
  uv run --extra scripts python scripts/seed_pages_from_crawl.py --no-s3
"""

from __future__ import annotations

import argparse
import mimetypes
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests
from bs4 import BeautifulSoup, Tag
from dotenv import load_dotenv
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

# (slug, title, source_url | None)
# None = no public source, admin must fill content manually
PAGES = [
    (
        "who-i-work-with",
        "Who I Serve",
        "https://www.collegemoneymethod.com/who-i-work-with/",
    ),
    (
        "resources",
        "Resources",
        "https://www.collegemoneymethod.com/resources/",
    ),
    (
        "counselor-resources",
        "Counselor Resources",
        "https://www.collegemoneymethod.com/counselor-resources/",
    ),
    (
        "high-school-curriculum",
        "High School Curriculum",
        "https://www.collegemoneymethod.com/high-school-curriculum/",
    ),
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# CSS selectors for elements to strip from extracted content
_STRIP_SELECTORS = [
    "script", "style", "noscript",
    ".sharedaddy", ".jp-relatedposts", ".wpcf7",
    ".entry-meta", ".entry-footer", "footer",
    ".wp-caption-text",
]

# WP color preset class names → hex values (from the site's global-styles CSS)
_WP_COLORS: dict[str, str] = {
    "primary": "#4f788d",     # teal
    "blue": "#6b9d81",        # CMM green (mis-named "blue" in WP)
    "light-violet": "#8fc4c6",
    "dark-blue": "#2c3847",
    "gray": "#9ea6ac",
    "light-gray": "#f3f9fd",
    "light-blue": "#f3f8fb",
    "white": "#ffffff",
    "black": "#000000",
}

# Regex that matches any WordPress /wp-content/uploads URL in href or src attributes
_WP_UPLOADS_RE = re.compile(
    r'(href|src)="(https?://(?:www\.)?collegemoneymethod\.com/wp-content/uploads/[^"]+)"',
    re.IGNORECASE,
)


# ── Block transforms ──────────────────────────────────────────────────────────

def _transform_wp_buttons(content: Tag) -> None:
    """
    Convert WordPress button blocks to inline-styled <a> tags so the button
    appearance survives class stripping.

    Reads has-{color}-background-color and has-{color}-color classes from the
    <a> element and maps them to hex values via _WP_COLORS. Falls back to the
    CMM teal (#4f788d) if no background class is found.
    """
    _BASE_STYLE = (
        "display:inline-block;"
        "color:#ffffff;"
        "border-radius:9999px;"
        "padding:10px 22px;"
        "font-size:1rem;"
        "font-weight:600;"
        "text-decoration:none;"
        "letter-spacing:0.5px;"
    )

    def _color_from_classes(classes: list[str], prefix: str, fallback: str) -> str:
        for cls in classes:
            m = re.match(rf"has-(.+)-{prefix}$", cls)
            if m:
                return _WP_COLORS.get(m.group(1), fallback)
        return fallback

    for btn_div in content.select(".wp-block-button"):
        link = btn_div.find("a")
        if not link or not isinstance(link, Tag):
            btn_div.decompose()
            continue

        classes = link.get("class") or []
        bg = _color_from_classes(classes, "background-color", "#4f788d")
        fg = _color_from_classes(classes, "color", "#ffffff")

        link["style"] = _BASE_STYLE + f"background-color:{bg};color:{fg};"
        # Remove all WP class attributes — inline style carries all needed styling
        del link["class"]

        # Replace the wrapper div with just the styled <a>
        btn_div.replace_with(link)

    # Unwrap .wp-block-buttons alignment containers — keep the children
    for wrapper in content.select(".wp-block-buttons"):
        wrapper.unwrap()


def _transform_getwid_tabs(content: Tag) -> None:
    """
    Convert Getwid tab blocks into a static two-column panel that retains the
    original tab-button look: left column = styled label buttons, right column
    = all tab bodies stacked with dividers.

    Using only inline styles so the block renders correctly as a Tiptap rawHtml
    node without any external CSS dependency.
    """
    _NAV_CELL = (
        "padding:12px 16px;"
        "font-family:Inter,sans-serif;font-size:0.78rem;font-weight:700;"
        "text-transform:uppercase;letter-spacing:0.8px;"
        "border-bottom:1px solid #dde4e9;"
        "white-space:nowrap;"
    )
    _NAV_ACTIVE = _NAV_CELL + "color:#4F788D;"
    _NAV_INACTIVE = _NAV_CELL + "color:#2C3847;"
    _NAV_LAST = _NAV_INACTIVE.replace("border-bottom:1px solid #dde4e9;", "border-bottom:none;")

    _BODY_CELL = (
        "flex:1;padding:14px 18px;font-size:0.95rem;color:#2C3847;"
        "border-bottom:1px solid #f0f4f6;"
    )
    _BODY_LAST = _BODY_CELL.replace("border-bottom:1px solid #f0f4f6;", "border-bottom:none;")

    _WRAPPER = (
        "display:flex;border:1px solid #dde4e9;border-radius:6px;"
        "overflow:hidden;margin:20px 0;"
    )
    _NAV_COL = (
        "min-width:130px;width:130px;border-right:1px solid #dde4e9;"
        "display:flex;flex-direction:column;"
    )
    _BODY_COL = "flex:1;display:flex;flex-direction:column;"

    for tabs_block in content.select(".wp-block-getwid-tabs"):
        titles = [
            el.get_text(strip=True)
            for el in tabs_block.select(".wp-block-getwid-tabs__title")
        ]
        bodies = [
            el.decode_contents()
            for el in tabs_block.select(".wp-block-getwid-tabs__tab-content")
        ]

        nav_items = ""
        body_items = ""
        last = len(titles) - 1
        for i, (title, body) in enumerate(zip(titles, bodies)):
            label_style = _NAV_ACTIVE if i == 0 else (_NAV_LAST if i == last else _NAV_INACTIVE)
            body_style = _BODY_LAST if i == last else _BODY_CELL
            nav_items += f'<div style="{label_style}">{title}</div>'
            body_items += f'<div style="{body_style}">{body}</div>'

        wrapper_html = (
            f'<div style="{_WRAPPER}">'
            f'<div style="{_NAV_COL}">{nav_items}</div>'
            f'<div style="{_BODY_COL}">{body_items}</div>'
            f"</div>"
        )
        wrapper = BeautifulSoup(wrapper_html, "lxml").find("div")
        tabs_block.replace_with(wrapper)


def _transform_image_boxes(content: Tag) -> None:
    """Strip Getwid image-box wrappers, keeping only the inner heading/text content."""
    for box in content.select(".wp-block-getwid-image-box"):
        inner = box.select_one(".wp-block-getwid-image-box__content")
        if inner:
            box.replace_with(inner)
        else:
            box.decompose()


# ── S3 asset migration ────────────────────────────────────────────────────────

def _s3_client():
    import boto3
    from src.config import settings
    return boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        region_name=settings.aws_region,
    ), settings


def _s3_key_for_wp_url(wp_url: str, page_slug: str) -> str:
    """Derive an S3 key from a WordPress uploads URL.

    e.g. .../wp-content/uploads/2022/01/foo.pdf → pages/high-school-curriculum/assets/foo.pdf
    """
    filename = Path(urlparse(wp_url).path).name
    return f"pages/{page_slug}/assets/{filename}"


def _build_existing_asset_map(conn) -> dict[str, str]:
    """
    Query storage_files for files already uploaded by migrate_wordpress_media.py.
    Returns {original_filename: s3_url} so we can reuse existing S3 URLs instead
    of re-uploading duplicates to a different key.
    """
    rows = conn.execute(
        text("SELECT original_filename, s3_url FROM storage_files")
    ).fetchall()
    return {row[0]: row[1] for row in rows}


def _migrate_wp_assets(
    html: str,
    page_slug: str,
    dry_run: bool,
    existing_assets: dict[str, str],
) -> str:
    """
    Find every WordPress /wp-content/uploads URL in the HTML, rewrite to S3.

    Lookup order:
      1. existing_assets map (files already migrated by migrate_wordpress_media.py)
      2. S3 head_object check on pages/{slug}/assets/{filename}
      3. Download from WordPress → upload fresh

    Only processes href/src attributes — skips srcset, CSS, and favicon links.
    Returns the updated HTML string.
    """
    s3, settings = _s3_client()
    bucket = settings.s3_bucket_name
    region = settings.aws_region

    url_cache: dict[str, str] = {}  # wp_url → s3_url (avoid duplicate uploads)

    def _replace(m: re.Match) -> str:
        attr, wp_url = m.group(1), m.group(2)

        # Skip resized image variants (e.g. foo-150x150.png) — use the original only
        if re.search(r"-\d+x\d+\.(png|jpg|jpeg|gif|webp)$", wp_url, re.IGNORECASE):
            return m.group(0)

        if wp_url in url_cache:
            return f'{attr}="{url_cache[wp_url]}"'

        filename = Path(urlparse(wp_url).path).name

        # 1. Reuse URL from prior migration if available
        if filename in existing_assets:
            s3_url = existing_assets[filename]
            print(f"    [s3] reuse existing: {filename} → {s3_url}")
            url_cache[wp_url] = s3_url
            return f'{attr}="{s3_url}"'

        # 2. Fresh upload to pages/{slug}/assets/{filename}
        s3_key = _s3_key_for_wp_url(wp_url, page_slug)
        s3_url = f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"

        if dry_run:
            print(f"    [dry-run] would upload: {filename} → s3://{bucket}/{s3_key}")
            url_cache[wp_url] = s3_url
            return f'{attr}="{s3_url}"'

        try:
            s3.head_object(Bucket=bucket, Key=s3_key)
            print(f"    [s3] exists: {s3_key}")
        except Exception:
            try:
                resp = requests.get(wp_url, timeout=30, headers=_HEADERS)
                resp.raise_for_status()
                content_type = (
                    resp.headers.get("Content-Type")
                    or mimetypes.guess_type(wp_url)[0]
                    or "application/octet-stream"
                )
                s3.put_object(
                    Bucket=bucket,
                    Key=s3_key,
                    Body=resp.content,
                    ContentType=content_type,
                )
                print(f"    [s3] uploaded: {s3_key} ({len(resp.content):,} bytes)")
            except Exception as exc:
                print(f"    [s3] WARN: failed to upload {wp_url}: {exc}")
                return m.group(0)  # keep original URL on failure

        url_cache[wp_url] = s3_url
        return f'{attr}="{s3_url}"'

    return _WP_UPLOADS_RE.sub(_replace, html)


# ── Page fetch ────────────────────────────────────────────────────────────────

def _fetch_page_html(
    url: str,
    page_slug: str,
    upload_to_s3: bool,
    dry_run: bool,
    existing_assets: dict[str, str] | None = None,
) -> str:
    """Fetch a WordPress page and return cleaned HTML.

    Converts Getwid tab blocks to static two-column panels.
    Optionally migrates WordPress assets to S3, reusing previously-migrated
    files from the existing_assets map (filename → s3_url).
    """
    resp = requests.get(url, timeout=15, headers=_HEADERS)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")

    content: Tag | None = (
        soup.select_one("article .entry-content")
        or soup.select_one(".entry-content")
        or soup.select_one("main article")
        or soup.select_one("main")
    )
    if not content:
        return ""

    # Transform blocks before stripping anything (order matters — buttons before strip)
    _transform_wp_buttons(content)
    _transform_getwid_tabs(content)
    _transform_image_boxes(content)

    # Strip unwanted elements
    for sel in _STRIP_SELECTORS:
        for el in content.select(sel):
            el.decompose()

    # Remove stray <meta> tags WP embeds inside content blocks
    for meta in content.find_all("meta"):
        meta.decompose()

    # Remove empty <p> tags left behind by decomposed nodes
    for p in content.find_all("p"):
        if not p.get_text(strip=True) and not p.find(True):
            p.decompose()

    html = str(content)

    if upload_to_s3:
        # Migrate WordPress assets to S3 and rewrite links
        html = _migrate_wp_assets(html, page_slug, dry_run, existing_assets or {})
    else:
        # Drop WP-hosted images when S3 upload is disabled
        html = re.sub(
            r'<img\b[^>]*src="https://(?:www\.)?collegemoneymethod\.com/wp-content/[^"]*"[^>]*/?>'
            , "", html, flags=re.IGNORECASE,
        )

    html = re.sub(r"\n{3,}", "\n\n", html).strip()
    return html


# ── Seeder ────────────────────────────────────────────────────────────────────

def seed(dry_run: bool, env: str, force: bool = False, upload_to_s3: bool = True) -> None:
    from src.pages.models import Page
    from src.config import settings  # noqa: F401 — triggers dotenv + validation

    load_dotenv(f".env.{env}" if env != "default" else ".env")

    engine = create_engine(settings.database_url)

    # Build a filename→s3_url map from files already migrated by migrate_wordpress_media.py
    # so we reuse those S3 URLs instead of re-uploading to a different key.
    existing_assets: dict[str, str] = {}
    if upload_to_s3:
        with engine.connect() as conn:
            existing_assets = _build_existing_asset_map(conn)
        print(f"  Loaded {len(existing_assets)} existing S3 assets from storage_files\n")

    with Session(engine) as session:
        for slug, title, url in PAGES:
            existing = session.scalar(select(Page).where(Page.slug == slug))
            if existing and not force:
                print(f"  skip   {slug!r} — already exists (use --force to overwrite)")
                continue

            content = ""
            if url:
                try:
                    content = _fetch_page_html(
                        url,
                        page_slug=slug,
                        upload_to_s3=upload_to_s3,
                        dry_run=dry_run,
                        existing_assets=existing_assets,
                    )
                    print(f"  fetch  {slug!r} — {len(content)} chars from {url}")
                except Exception as exc:
                    print(f"  WARN   {slug!r} — fetch failed ({exc}), starting blank")
            else:
                print(f"  skip   {slug!r} — no source URL, starting blank")

            if dry_run:
                action = "update" if existing else "insert"
                print(f"          [dry-run] would {action}: slug={slug!r}, title={title!r}")
                continue

            if existing:
                existing.title = title
                existing.content = content or None
                print(f"  update {slug!r}")
            else:
                session.add(Page(slug=slug, title=title, content=content or None, status="draft"))

        if not dry_run:
            session.commit()
            print("\nDone — pages committed.")
        else:
            print("\n[dry-run] No changes written.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed CMS pages from live site HTML.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--force", action="store_true", help="Overwrite existing pages")
    parser.add_argument("--env", default="default", help="Env file suffix (dev, prod)")
    parser.add_argument(
        "--no-s3",
        action="store_true",
        help="Skip S3 asset migration (WordPress image links will be stripped instead)",
    )
    args = parser.parse_args()
    seed(dry_run=args.dry_run, env=args.env, force=args.force, upload_to_s3=not args.no_s3)


if __name__ == "__main__":
    main()
