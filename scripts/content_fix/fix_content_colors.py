"""
Fix off-palette colors in content bodies (TipTap JSON) on the DEV database, per
the CMM design-guidelines audit. Operates on `topics` AND `content_assets`.

Replacements (case-insensitive, whole hex):
    #586068 -> #2C3E4A   (off-palette slate-gray text  -> Dark Slate)   [topics]
    #e6b830 -> #8A6A0F   (undocumented darkened-gold    -> gold accent) [topics]
    #4b8c91 -> #4F788D   (off-palette teal background   -> Teal)        [content_assets]

Pure-white card backgrounds (#FFFFFF) are intentionally KEPT (per product decision).

Re-running on already-fixed rows is a no-op (old hexes no longer present).
Dry-run by default (prints per-row counts, writes nothing). Pass --apply to commit.
Target DB: DEV DATABASE_URL from .env.dev (override: --database-url / env DATABASE_URL).

Usage:
    uv run python -m scripts.content_fix.fix_content_colors                    # dry-run, both tables
    uv run python -m scripts.content_fix.fix_content_colors --apply            # commit
    uv run python -m scripts.content_fix.fix_content_colors --table content_assets --apply
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.db.base import get_engine  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent.parent

# old-hex (lowercased) -> new-hex. Applied case-insensitively.
COLOR_MAP = {
    "#586068": "#2C3E4A",
    "#e6b830": "#8A6A0F",
    "#4b8c91": "#4F788D",
}

# table -> label column used for per-row output.
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


def apply_color_map(content: str) -> tuple[str, dict[str, int]]:
    """Replace each old hex (any case) with its new hex. Returns (new, per-color counts)."""
    counts: dict[str, int] = {}
    out = content
    for old, new in COLOR_MAP.items():
        pattern = re.compile(re.escape(old), re.IGNORECASE)
        out, n = pattern.subn(new, out)
        if n:
            counts[old] = n
    return out, counts


def process_table(conn, table: str, label_col: str, apply: bool) -> tuple[int, dict[str, int]]:
    """Apply color map to one table. Returns (rows_changed, per-color totals)."""
    rows = conn.execute(text(
        f"SELECT id, {label_col} AS label, content FROM {table} "
        f"WHERE content IS NOT NULL ORDER BY {label_col}"
    )).mappings().all()

    changed = 0
    totals: dict[str, int] = {}
    for row in rows:
        new_content, counts = apply_color_map(row["content"])
        if not counts:
            continue
        changed += 1
        for c, n in counts.items():
            totals[c] = totals.get(c, 0) + n
        summary = ", ".join(f"{c}->{COLOR_MAP[c]} x{n}" for c, n in counts.items())
        print(f"  [{table}] {row['label']}: {summary}")
        if apply:
            conn.execute(
                text(f"UPDATE {table} SET content = :c, updated_at = now() WHERE id = :id"),
                {"c": new_content, "id": row["id"]},
            )
    return changed, totals


def main() -> None:
    parser = argparse.ArgumentParser(description="Fix off-palette colors in dev content bodies.")
    parser.add_argument("--database-url", dest="database_url", default=None)
    parser.add_argument("--table", choices=[*TABLES, "both"], default="both")
    parser.add_argument("--apply", action="store_true", help="Commit changes (default: dry-run).")
    args = parser.parse_args()

    db_url = resolve_database_url(args.database_url)
    print(f"Connecting to: {re.sub(r'//[^@]+@', '//***@', db_url)}")
    print(f"Mode: {'APPLY (will commit)' if args.apply else 'DRY-RUN (no writes)'}\n")

    tables = list(TABLES) if args.table == "both" else [args.table]
    engine = get_engine(db_url)
    total_changed = 0
    grand: dict[str, int] = {}

    with engine.begin() as conn:
        for table in tables:
            changed, totals = process_table(conn, table, TABLES[table], args.apply)
            total_changed += changed
            for c, n in totals.items():
                grand[c] = grand.get(c, 0) + n
        if not args.apply:
            conn.rollback()  # explicit: begin() would otherwise commit on exit

    print(f"\nRows affected: {total_changed}")
    for c, n in grand.items():
        print(f"  {c} -> {COLOR_MAP[c]}: {n} occurrences")
    if not args.apply:
        print("\nDry-run only. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
