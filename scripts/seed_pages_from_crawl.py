#!/usr/bin/env python3
"""
Seed CMS pages from the Firecrawl output in scripts/output/crawl/.

Creates draft pages for the 4 core marketing pages. Pages with missing
crawl content (502 errors) are seeded as empty drafts ready to edit.

Usage (from project root):
  uv run --extra scripts python scripts/seed_pages_from_crawl.py
  uv run --extra scripts python scripts/seed_pages_from_crawl.py --dry-run
  uv run --extra scripts python scripts/seed_pages_from_crawl.py --env dev
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import markdown as md_lib
from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

CRAWL_DIR = Path(__file__).parent / "output" / "crawl"

# Core pages to seed: (slug, title, crawl_filename | None)
PAGES = [
    (
        "who-i-work-with",
        "Who I Serve",
        "https-____www-collegemoneymethod-com__who-i-work-with.md",
    ),
    (
        "resources",
        "Resources",
        "https-____www-collegemoneymethod-com__resources.md",
    ),
    (
        "counselor-resources",
        "Counselor Resources",
        None,  # 502 on crawl — start blank
    ),
    (
        "high-school-curriculum",
        "High School Curriculum",
        None,  # 502 on crawl — start blank
    ),
]


def _parse_crawl_markdown(path: Path) -> str:
    """Strip crawl header (everything up to and including the --- separator), convert to HTML."""
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()

    # The crawl header always ends with a `---` line; take everything after it.
    try:
        sep_idx = lines.index("---")
        body_lines = lines[sep_idx + 1:]
    except ValueError:
        body_lines = lines  # no separator found — use all content

    body = "\n".join(body_lines).strip()

    # Drop "Skip to content" nav links
    body = re.sub(r"\[Skip to content\]\([^)]*\)\n?", "", body)

    # Drop WordPress image lines (no alt text, not useful in new site)
    body = re.sub(r"!\[\]\(https://www\.collegemoneymethod\.com/wp-content/[^\)]+\)\n?", "", body)

    # Convert markdown → HTML
    html = md_lib.markdown(body, extensions=["extra", "nl2br"])
    return html.strip()


def seed(dry_run: bool, env: str, force: bool = False) -> None:
    from src.pages.models import Page
    from src.config import settings

    load_dotenv(f".env.{env}" if env != "default" else ".env")

    engine = create_engine(settings.database_url)

    with Session(engine) as session:
        for slug, title, filename in PAGES:
            existing = session.scalar(select(Page).where(Page.slug == slug))
            if existing and not force:
                print(f"  skip  {slug!r} — already exists (use --force to overwrite)")
                continue

            content = ""
            if filename:
                crawl_path = CRAWL_DIR / filename
                if crawl_path.exists():
                    content = _parse_crawl_markdown(crawl_path)
                    print(f"  seed  {slug!r} — {len(content)} chars from crawl")
                else:
                    print(f"  seed  {slug!r} — crawl file not found, starting blank")
            else:
                print(f"  seed  {slug!r} — no crawl source, starting blank")

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
    parser = argparse.ArgumentParser(description="Seed CMS pages from crawl output.")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--force", action="store_true", help="Overwrite existing pages")
    parser.add_argument("--env", default="default", help="Env file suffix (dev, prod)")
    args = parser.parse_args()
    seed(dry_run=args.dry_run, env=args.env, force=args.force)


if __name__ == "__main__":
    main()
