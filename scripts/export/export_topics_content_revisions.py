"""
Export every Topic's content body from the DEV database as TipTap JSON, for
later content-revision analysis (design-guideline / font / color / URL checks).

Topics.content is stored as a TipTap ProseMirror doc (JSON string). This script
dumps one pretty-printed `<slug>.json` per topic plus a `_manifest.json` that
summarizes node types, raw-HTML blocks, and every URL found — so a reviewer can
quickly spot inline HTML that breaks CMM design guidelines or links still
pointing at the old site.

Target DB: DEV. The dev DATABASE_URL is read from `.env.dev` by default.
Override with `--database-url` or the DATABASE_URL env var.

Usage:
    uv run python -m scripts.export.export_topics_content_revisions
    uv run python -m scripts.export.export_topics_content_revisions --database-url "postgresql://..."
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.db.base import get_engine  # noqa: E402

OUTPUT_DIR = Path(__file__).parent.parent / "output" / "topics_revisions"
PROJECT_ROOT = Path(__file__).parent.parent.parent

# Matches href="..." / href='...' inside raw HTML blocks.
HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)


def resolve_database_url(cli_url: str | None) -> str:
    """DEV url precedence: --database-url > env DATABASE_URL > .env.dev."""
    if cli_url:
        return cli_url
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    dev_url = dotenv_values(PROJECT_ROOT / ".env.dev").get("DATABASE_URL")
    if not dev_url:
        raise SystemExit("No DATABASE_URL found (checked --database-url, env, .env.dev).")
    return dev_url


def parse_content(raw: str | None) -> dict | None:
    """Parse the stored content into a TipTap doc dict, or None if unparseable."""
    if not raw or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def walk(node: dict, type_counts: Counter, urls: set[str]) -> None:
    """Recursively tally node types and collect every URL (link marks + raw HTML)."""
    if not isinstance(node, dict):
        return
    node_type = node.get("type")
    if node_type:
        type_counts[node_type] += 1

    # Link marks: {"type":"link","attrs":{"href":"..."}}
    for mark in node.get("marks") or []:
        if isinstance(mark, dict) and mark.get("type") == "link":
            href = (mark.get("attrs") or {}).get("href")
            if href:
                urls.add(href)

    # Raw HTML blocks: {"type":"rawHtml","attrs":{"html":"<... href=...>"}}
    if node_type == "rawHtml":
        html = (node.get("attrs") or {}).get("html", "")
        urls.update(HREF_RE.findall(html))

    for child in node.get("content") or []:
        walk(child, type_counts, urls)


def summarize(doc: dict) -> tuple[dict[str, int], list[str]]:
    type_counts: Counter = Counter()
    urls: set[str] = set()
    walk(doc, type_counts, urls)
    return dict(sorted(type_counts.items())), sorted(urls)


def main() -> None:
    parser = argparse.ArgumentParser(description="Export dev Topics content as TipTap JSON.")
    parser.add_argument("--database-url", dest="database_url", default=None)
    args = parser.parse_args()

    db_url = resolve_database_url(args.database_url)
    # Redact credentials in the log line.
    safe_url = re.sub(r"//[^@]+@", "//***@", db_url)
    print(f"Connecting to: {safe_url}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    engine = get_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(text(
            """
            SELECT id, title, slug, status, goal_id, updated_at, content
            FROM topics
            ORDER BY sort_order, title
            """
        )).mappings().all()

    print(f"Fetched {len(rows)} topics.")
    manifest: list[dict] = []
    skipped: list[str] = []

    for row in rows:
        slug = row["slug"] or str(row["id"])
        doc = parse_content(row["content"])

        if doc is None:
            skipped.append(slug)
            type_counts, urls, raw_html_count = {}, [], 0
        else:
            type_counts, urls = summarize(doc)
            raw_html_count = type_counts.get("rawHtml", 0)
            (OUTPUT_DIR / f"{slug}.json").write_text(
                json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        manifest.append({
            "id": str(row["id"]),
            "title": row["title"],
            "slug": slug,
            "status": row["status"],
            "goal_id": str(row["goal_id"]) if row["goal_id"] else None,
            "updated_at": row["updated_at"].isoformat() if row["updated_at"] else None,
            "content_chars": len(row["content"] or ""),
            "parsed": doc is not None,
            "node_type_counts": type_counts,
            "raw_html_blocks": raw_html_count,
            "urls": urls,
        })

    (OUTPUT_DIR / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    total_raw_html = sum(m["raw_html_blocks"] for m in manifest)
    total_urls = len({u for m in manifest for u in m["urls"]})
    print(f"Wrote {len(rows) - len(skipped)} topic files -> {OUTPUT_DIR}")
    print(f"Manifest: {len(manifest)} entries | raw-HTML blocks: {total_raw_html} | unique URLs: {total_urls}")
    if skipped:
        print(f"Skipped (empty/unparseable content): {', '.join(skipped)}")


if __name__ == "__main__":
    main()
