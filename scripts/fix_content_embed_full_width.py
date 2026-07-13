"""
Make embedded-table (baserow) blocks full content width, dropping the colored
callout / button wrapper. Pattern-based so it works on ANY database (local, dev,
prod) regardless of asset id.

For every rawHtml block whose html embeds a baserow.io iframe, the block is
replaced with a clean full-width version: a right-aligned "View Fullscreen" link
(Navy Ink) above a `width:100%` iframe, no teal callout background/button.

Runs on `topics` and `content_assets`. Idempotent (re-running yields the same
block). Dry-run by default; pass --apply to commit.

Target DB: DEV DATABASE_URL from .env.dev by default. Point elsewhere with
--database-url (e.g. local: postgresql://postgres:postgres@localhost:54322/postgres)
or the DATABASE_URL env var.

Usage:
    uv run python -m scripts.fix_content_embed_full_width                       # dry-run, dev
    uv run python -m scripts.fix_content_embed_full_width --apply               # commit, dev
    uv run python -m scripts.fix_content_embed_full_width \
        --database-url postgresql://postgres:postgres@localhost:54322/postgres --apply
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

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.db.base import get_engine  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent
TABLES = {"topics": "slug", "content_assets": "name"}

# Pull the baserow embed URL out of an <iframe src="...">.
BASEROW_SRC_RE = re.compile(r"""<iframe[^>]+src=["']?(https://baserow\.io/[^"'\s>]+)""", re.IGNORECASE)


def resolve_database_url(cli_url: str | None) -> str:
    if cli_url:
        return cli_url
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    dev_url = dotenv_values(PROJECT_ROOT / ".env.dev").get("DATABASE_URL")
    if not dev_url:
        raise SystemExit("No DATABASE_URL found (checked --database-url, env, .env.dev).")
    return dev_url


def build_embed_html(url: str) -> str:
    """Clean, full-width embed: dark 'View Fullscreen' link over a 100%-width iframe."""
    return (
        '<div style="margin:16px 0;">\n'
        '  <div style="display: flex; justify-content: flex-end; margin-bottom: 10px;">\n'
        f'    <a href="{url}" target="_blank" rel="noopener" '
        'style="color: #1E3A5F; text-decoration: underline;">View Fullscreen</a>\n'
        '  </div>\n'
        f'  <iframe src="{url}" width="100%" height="900" '
        'style="border: none; overflow: hidden;" loading="lazy"></iframe>\n'
        '</div>'
    )


def rewrite_embeds(node: dict) -> int:
    """Recursively rewrite baserow embed rawHtml blocks in place. Returns count changed."""
    changed = 0
    if not isinstance(node, dict):
        return 0
    if node.get("type") == "rawHtml":
        html = (node.get("attrs") or {}).get("html", "")
        if "baserow.io" in html:
            m = BASEROW_SRC_RE.search(html)
            if m:
                new_html = build_embed_html(m.group(1))
                if new_html != html:
                    node["attrs"]["html"] = new_html
                    changed += 1
    for child in node.get("content") or []:
        changed += rewrite_embeds(child)
    return changed


def process_table(conn, table: str, label_col: str, apply: bool) -> int:
    rows = conn.execute(text(
        f"SELECT id, {label_col} AS label, content FROM {table} "
        f"WHERE content IS NOT NULL AND content LIKE '%baserow.io%'"
    )).mappings().all()

    changed_rows = 0
    for row in rows:
        try:
            doc = json.loads(row["content"])
        except json.JSONDecodeError:
            continue
        n = rewrite_embeds(doc)
        if not n:
            continue
        changed_rows += 1
        print(f"  [{table}] {row['label']}: {n} embed block(s) → full width")
        if apply:
            conn.execute(
                text(f"UPDATE {table} SET content = :c, updated_at = now() WHERE id = :id"),
                {"c": json.dumps(doc, ensure_ascii=False), "id": row["id"]},
            )
    return changed_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Make baserow embeds full width.")
    parser.add_argument("--database-url", dest="database_url", default=None)
    parser.add_argument("--table", choices=[*TABLES, "both"], default="both")
    parser.add_argument("--apply", action="store_true", help="Commit changes (default: dry-run).")
    args = parser.parse_args()

    db_url = resolve_database_url(args.database_url)
    print(f"Connecting to: {re.sub(r'//[^@]+@', '//***@', db_url)}")
    print(f"Mode: {'APPLY (will commit)' if args.apply else 'DRY-RUN (no writes)'}\n")

    tables = list(TABLES) if args.table == "both" else [args.table]
    engine = get_engine(db_url)
    total = 0
    with engine.begin() as conn:
        for table in tables:
            total += process_table(conn, table, TABLES[table], args.apply)
        if not args.apply:
            conn.rollback()

    print(f"\nRows changed: {total}")
    if not args.apply:
        print("Dry-run only. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
