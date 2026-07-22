"""
Export school Resource Center preview links + passwords to CSV.

Produces a CSV with columns:
    School Name, School Resource Center Preview link, School Password

The preview link mirrors the {{resource_center_url}} merge tag the app builds in
app/routes/hub/communications.tsx:

    https://next.collegemoneymethod.com/school/<school.slug>

The base is fixed to the production frontend URL. The password is
`schools.cmm_website_password` (the {{resource_center_password}} merge tag). Only
CURRENT CUSTOMER schools (is_current_customer = true) that have a slug are
included — without a slug there is no resolvable resource center URL. Schools
with no password emit an empty password cell.

Target DB: PROD by default. Override with --env dev or an explicit --database-url.

Usage:
    uv run --env-file=.env.prod python -m scripts.export.export_school_resource_center_links
    uv run python -m scripts.export.export_school_resource_center_links --env dev
    uv run python -m scripts.export.export_school_resource_center_links --database-url "postgresql://..."
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from src.db.base import get_engine  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent.parent
OUTPUT_DIR = Path(__file__).parent.parent / "output"
# Fixed production frontend base — the resource center preview links must always
# point at production regardless of which DB env is queried.
FRONTEND_URL = "https://next.collegemoneymethod.com"

QUERY = text(
    """
    SELECT s.name AS school_name, s.slug, s.cmm_website_password
    FROM schools s
    WHERE s.is_current_customer = true
      AND s.slug IS NOT NULL AND s.slug <> ''
    ORDER BY s.name
    """
)


def resolve_database_url(cli_url: str | None, env: str) -> str:
    """URL precedence: --database-url > env DATABASE_URL > .env.<env>."""
    if cli_url:
        return cli_url
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env_file = PROJECT_ROOT / f".env.{env}"
    url = dotenv_values(env_file).get("DATABASE_URL")
    if not url:
        raise SystemExit(
            f"No DATABASE_URL found (checked --database-url, env, {env_file.name})."
        )
    return url


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export current-customer school resource center links + passwords to CSV."
    )
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default="prod",
        help="Which .env file to read DATABASE_URL / FRONTEND_URL from (default: prod).",
    )
    parser.add_argument("--database-url", dest="database_url", default=None)
    args = parser.parse_args()

    db_url = resolve_database_url(args.database_url, args.env)

    safe_url = re.sub(r"//[^@]+@", "//***@", db_url)  # redact credentials in logs
    print(f"[{args.env}] Connecting to: {safe_url}")
    print(f"[{args.env}] Preview link base: {FRONTEND_URL}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    engine = get_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(QUERY).mappings().all()

    print(f"Fetched {len(rows)} current-customer schools with a slug.")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = OUTPUT_DIR / f"school_resource_center_links_{args.env}_{timestamp}.csv"

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["School Name", "School Resource Center Preview link", "School Password"]
        )
        for row in rows:
            preview_link = f"{FRONTEND_URL}/school/{row['slug']}"
            writer.writerow(
                [
                    row["school_name"],
                    preview_link,
                    row["cmm_website_password"] or "",
                ]
            )

    print(f"Wrote {len(rows)} rows -> {out_path}")


if __name__ == "__main__":
    main()
