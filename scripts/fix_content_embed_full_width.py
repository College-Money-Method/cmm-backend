"""
Make embedded-table (baserow) blocks full content width on the DEV database.

Two data resources wrapped their baserow iframe at width:80%; `e7f57b7e` also
wrapped it in a teal callout (`background-color:#4F788D`). Per review, the
callout is unwanted and the table should span the full content width. This
replaces the whole rawHtml embed node with a clean full-width version.

Dry-run by default; pass --apply to commit.
Target DB: DEV DATABASE_URL from .env.dev (override: --database-url / DATABASE_URL).

Usage:
    uv run python -m scripts.fix_content_embed_full_width
    uv run python -m scripts.fix_content_embed_full_width --apply
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

# asset id -> full-width replacement HTML for its baserow embed block.
MERIT_URL = "https://baserow.io/public/grid/85yPgz47Z_VGf3a0UcIBF-FZeoj-Qr77IBZVJaiZsaM"
NEED_URL = "https://baserow.io/public/grid/VUHypa6XjmuRbuxXjzL8czkt_0gU7N-H0ixgdM7iJ5o"

EMBED_HTML = {
    # Merit-based Aid Data — drop the teal callout, full width, dark link.
    "e7f57b7e-e7a5-43ba-a783-61832e7a8a85": (
        '<div style="margin:16px 0;">\n'
        '  <div style="display: flex; justify-content: flex-end; margin-bottom: 10px;">\n'
        f'    <a href="{MERIT_URL}" target="_blank" rel="noopener" '
        'style="color: #1E3A5F; text-decoration: underline;">View Fullscreen</a>\n'
        '  </div>\n'
        f'  <iframe src="{MERIT_URL}" width="100%" height="900" '
        'style="border: none; overflow: hidden;" loading="lazy"></iframe>\n'
        '</div>'
    ),
    # Need-based Financial Aid Data — same layout, just full width (keep teal button).
    "eab44ea4-3fed-4f12-b9c7-71f2663af3ea": (
        '<div style="width: 100%;">\n'
        '  <div style="display: flex; justify-content: flex-end; margin-bottom: 10px;">\n'
        f'    <a href="{NEED_URL}" target="_blank" rel="noopener" '
        'style="text-decoration: none; background: #4F788D; color: white; padding: 8px 14px; '
        'border-radius: 6px; font-family: sans-serif; font-size: 14px;">View Fullscreen</a>\n'
        '  </div>\n'
        f'  <iframe src="{NEED_URL}" width="100%" height="900" '
        'style="border: none; overflow: hidden;" loading="lazy"></iframe>\n'
        '</div>'
    ),
}


def resolve_database_url(cli_url: str | None) -> str:
    if cli_url:
        return cli_url
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    dev_url = dotenv_values(PROJECT_ROOT / ".env.dev").get("DATABASE_URL")
    if not dev_url:
        raise SystemExit("No DATABASE_URL found (checked --database-url, env, .env.dev).")
    return dev_url


def replace_embed(doc: dict, new_html: str) -> bool:
    """Set the html of the rawHtml node containing the baserow iframe. Returns True if changed."""
    for node in doc.get("content") or []:
        if node.get("type") == "rawHtml" and "baserow.io" in (node.get("attrs") or {}).get("html", ""):
            node["attrs"]["html"] = new_html
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Make baserow embeds full width on dev.")
    parser.add_argument("--database-url", dest="database_url", default=None)
    parser.add_argument("--apply", action="store_true", help="Commit changes (default: dry-run).")
    args = parser.parse_args()

    db_url = resolve_database_url(args.database_url)
    print(f"Connecting to: {re.sub(r'//[^@]+@', '//***@', db_url)}")
    print(f"Mode: {'APPLY (will commit)' if args.apply else 'DRY-RUN (no writes)'}\n")

    engine = get_engine(db_url)
    with engine.begin() as conn:
        for asset_id, new_html in EMBED_HTML.items():
            row = conn.execute(
                text("SELECT name, content FROM content_assets WHERE id = :id"),
                {"id": asset_id},
            ).mappings().first()
            if not row:
                print(f"  {asset_id}: NOT FOUND")
                continue
            doc = json.loads(row["content"])
            changed = replace_embed(doc, new_html)
            print(f"  {row['name']}: {'embed rewritten' if changed else 'no baserow block found'}")
            if changed and args.apply:
                conn.execute(
                    text("UPDATE content_assets SET content = :c, updated_at = now() WHERE id = :id"),
                    {"c": json.dumps(doc, ensure_ascii=False), "id": asset_id},
                )
        if not args.apply:
            conn.rollback()

    if not args.apply:
        print("\nDry-run only. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
