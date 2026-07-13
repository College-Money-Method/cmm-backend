#!/usr/bin/env python3
"""
Crawl collegemoneymethod.com and save each page as markdown.

Saves output to scripts/output/crawl/ — one .md file per page, plus an
index.json with the full page list (url, title, path).

Prerequisites:
  - FIRECRAWL_API_KEY in .env

Usage (from project root):
  uv run --extra scripts python scripts/crawl_marketing_site.py
  uv run --extra scripts python scripts/crawl_marketing_site.py --limit 50
  uv run --extra scripts python scripts/crawl_marketing_site.py --url https://collegemoneymethod.com/about
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv  # noqa: E402 — loaded after sys.path fix

load_dotenv()

FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY")
if not FIRECRAWL_API_KEY:
    print("ERROR: FIRECRAWL_API_KEY not set in .env")
    sys.exit(1)

OUTPUT_DIR = Path(__file__).parent / "output" / "crawl"
BASE_URL = "https://collegemoneymethod.com"


def slugify(url: str) -> str:
    """Convert a URL to a safe filename stem."""
    path = url.replace(BASE_URL, "").strip("/") or "home"
    path = re.sub(r"[^a-zA-Z0-9/_-]", "-", path)
    path = re.sub(r"-+", "-", path).strip("-")
    return path.replace("/", "__") or "home"


def crawl(start_url: str, limit: int) -> None:
    from firecrawl import FirecrawlApp  # imported here so missing dep gives clear error

    app = FirecrawlApp(api_key=FIRECRAWL_API_KEY)

    print(f"Crawling {start_url} (limit={limit}) …")
    result = app.crawl_url(
        start_url,
        limit=limit,
        scrape_options={"formats": ["markdown"]},
    )

    pages = result.data if hasattr(result, "data") else result
    if not pages:
        print("No pages returned — check the URL or API key.")
        sys.exit(1)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    index = []
    for page in pages:
        meta = page.metadata if hasattr(page, "metadata") else {}
        if hasattr(meta, "url"):
            url = meta.url or ""
            title = getattr(meta, "title", None) or url
        else:
            url = meta.get("url", "")
            title = meta.get("title", None) or url
        markdown = page.markdown if hasattr(page, "markdown") else page.get("markdown", "")

        slug = slugify(url)
        out_path = OUTPUT_DIR / f"{slug}.md"
        out_path.write_text(f"# {title}\n\nSource: {url}\n\n---\n\n{markdown}", encoding="utf-8")

        index.append({"url": url, "title": title, "file": out_path.name})
        print(f"  ✓  {url}")

    index_path = OUTPUT_DIR / "index.json"
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nDone — {len(pages)} pages saved to {OUTPUT_DIR}/")
    print(f"Index: {index_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Crawl the CMM marketing site via Firecrawl.")
    parser.add_argument("--url", default=BASE_URL, help="Starting URL (default: site root)")
    parser.add_argument("--limit", type=int, default=100, help="Max pages to crawl (default: 100)")
    args = parser.parse_args()
    crawl(args.url, args.limit)


if __name__ == "__main__":
    main()
