"""
Export counselor login credentials to CSV for a bulk credentials email.

Produces a CSV with columns: firstname, email, school, password.

Only counselors belonging to CURRENT CUSTOMER schools are included
(schools.is_current_customer = true). The password reproduces the exact scheme
the accounts were provisioned with in Supabase Auth via
`src.auth.hub_password.default_hub_password`:

    password = <email handle> + <school.cmm_website_password>

(the same derivation used by scripts/seed/seed_counselors_from_contacts.py).
Contacts without an email, without a school, or deactivated (deleted_at set) are
skipped. Schools with no cmm_website_password ARE included — their counselor
password is just the email handle (no suffix), matching how those accounts were
provisioned. Duplicate emails are emitted once.

Target DB: PROD by default — these are the real credentials customers will use
to log in, so they must reflect production. Override with --env dev or an
explicit --database-url.

Usage:
    uv run python -m scripts.export.export_counselor_credentials
    uv run python -m scripts.export.export_counselor_credentials --env dev
    uv run python -m scripts.export.export_counselor_credentials --database-url "postgresql://..."
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

from src.auth.hub_password import default_hub_password  # noqa: E402
from src.db.base import get_engine  # noqa: E402

PROJECT_ROOT = Path(__file__).parent.parent.parent
OUTPUT_DIR = Path(__file__).parent.parent / "output"

# Every current-customer counselor plus the pieces needed to rebuild the login
# password. Schools with a NULL/empty cmm_website_password are kept — their
# password becomes just the email handle (default_hub_password handles this).
QUERY = text(
    """
    SELECT c.first_name, c.email, s.name AS school_name, s.cmm_website_password
    FROM contacts c
    JOIN schools s ON s.id = c.school_id
    WHERE s.is_current_customer = true
      AND c.deleted_at IS NULL
      AND c.email IS NOT NULL AND c.email <> ''
    ORDER BY s.name, c.first_name
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
        description="Export current-customer counselor credentials to CSV."
    )
    parser.add_argument(
        "--env",
        choices=["dev", "prod"],
        default="prod",
        help="Which .env file to read DATABASE_URL from (default: prod).",
    )
    parser.add_argument("--database-url", dest="database_url", default=None)
    args = parser.parse_args()

    db_url = resolve_database_url(args.database_url, args.env)
    safe_url = re.sub(r"//[^@]+@", "//***@", db_url)  # redact credentials in logs
    print(f"[{args.env}] Connecting to: {safe_url}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    engine = get_engine(db_url)
    with engine.connect() as conn:
        rows = conn.execute(QUERY).mappings().all()

    print(f"Fetched {len(rows)} current-customer counselor contacts.")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_path = OUTPUT_DIR / f"counselor_credentials_{args.env}_{timestamp}.csv"

    seen_emails: set[str] = set()
    written = 0
    duplicates = 0

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["firstname", "email", "school", "password"])

        for row in rows:
            # Lowercase to match how the seed script normalized emails before
            # deriving/setting the Supabase Auth password.
            email = (row["email"] or "").strip().lower()
            if email in seen_emails:
                duplicates += 1
                continue
            seen_emails.add(email)

            password = default_hub_password(email, row["cmm_website_password"])

            writer.writerow([
                (row["first_name"] or "").strip(),
                email,
                row["school_name"],
                password,
            ])
            written += 1

    print(f"Wrote {written} rows -> {out_path}")
    if duplicates:
        print(f"Skipped {duplicates} duplicate email(s).")


if __name__ == "__main__":
    main()
