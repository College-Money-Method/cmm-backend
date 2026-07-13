#!/usr/bin/env python3
"""
Find Airtable contacts that are not in Supabase auth.users.

Fetches all Airtable Contacts records, queries Supabase for existing user emails,
and outputs the diff to CSV + prints a summary.

Usage (from project root):
  uv run python scripts/analyze_airtable_contacts_missing_from_supabase.py
  uv run python scripts/analyze_airtable_contacts_missing_from_supabase.py --csv-out airtable_csv_export/Contacts.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config import settings
from src.integrations.airtable import get_contacts_records, get_schools_records
from supabase import create_client


def fetch_supabase_emails(supabase_url: str, service_role_key: str) -> set[str]:
    """Return lowercase set of all emails in auth.users via service-role key."""
    client = create_client(supabase_url, service_role_key)
    # admin.list_users returns paginated results; iterate all pages
    all_emails: set[str] = set()
    page = 1
    per_page = 1000
    while True:
        response = client.auth.admin.list_users(page=page, per_page=per_page)
        if not response:
            break
        for user in response:
            if user.email:
                all_emails.add(user.email.strip().lower())
        if len(response) < per_page:
            break
        page += 1
    return all_emails


def build_school_name_lookup(at_schools: list[dict]) -> dict[str, str]:
    """Map Airtable school rec ID → school name."""
    return {rec["id"]: (rec.get("fields", {}).get("School") or rec["id"]) for rec in at_schools}


def main() -> None:
    parser = argparse.ArgumentParser(description="Find Airtable contacts missing from Supabase")
    parser.add_argument(
        "--csv-out",
        default="airtable_csv_export/Contacts.csv",
        help="Path to write full Airtable contacts CSV (default: airtable_csv_export/Contacts.csv)",
    )
    parser.add_argument(
        "--missing-out",
        default="airtable_csv_export/Contacts_missing_from_supabase.csv",
        help="Path to write missing-contacts CSV",
    )
    args = parser.parse_args()

    print("Fetching Airtable contacts…")
    at_contacts = get_contacts_records()
    print(f"  → {len(at_contacts)} total Airtable contacts")

    print("Fetching Airtable schools (for name lookup)…")
    at_schools = get_schools_records()
    school_name_by_id = build_school_name_lookup(at_schools)

    print("Fetching Supabase users…")
    supabase_emails = fetch_supabase_emails(settings.supabase_url, settings.supabase_service_role_key)
    print(f"  → {len(supabase_emails)} Supabase users")

    # ── Export full contacts CSV ───────────────────────────────────────────────
    csv_path = Path(args.csv_out)
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    all_rows: list[dict] = []
    for rec in at_contacts:
        fields = rec.get("fields", {})
        email = (fields.get("Email") or "").strip()
        linked_schools: list[str] = fields.get("Sch") or []
        school_names = "; ".join(school_name_by_id.get(sid, sid) for sid in linked_schools)
        role = fields.get("Role") or ""
        first = fields.get("First Name") or fields.get("First") or ""
        last = fields.get("Last Name") or fields.get("Last") or ""
        name = fields.get("Name") or f"{first} {last}".strip()
        in_supabase = email.lower() in supabase_emails if email else False
        all_rows.append({
            "airtable_id": rec["id"],
            "name": name,
            "email": email,
            "role": role,
            "school_names": school_names,
            "has_school_link": "yes" if linked_schools else "no",
            "in_supabase": "yes" if in_supabase else ("no" if email else "no_email"),
        })

    fieldnames = ["airtable_id", "name", "email", "role", "school_names", "has_school_link", "in_supabase"]
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"\nFull contacts CSV written to: {csv_path}")

    # ── Missing contacts analysis ──────────────────────────────────────────────
    missing_rows = [r for r in all_rows if r["in_supabase"] == "no" and r["email"]]
    no_email_rows = [r for r in all_rows if not r["email"]]

    # Breakdown of missing rows
    missing_no_school = [r for r in missing_rows if r["has_school_link"] == "no"]
    missing_with_school = [r for r in missing_rows if r["has_school_link"] == "yes"]

    missing_path = Path(args.missing_out)
    missing_path.parent.mkdir(parents=True, exist_ok=True)
    with open(missing_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(missing_rows)
    print(f"Missing contacts CSV written to: {missing_path}")

    # ── Print summary ──────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"Total Airtable contacts:         {len(at_contacts)}")
    print(f"  With email:                    {len([r for r in all_rows if r['email']])}")
    print(f"  Without email:                 {len(no_email_rows)}")
    print(f"Total Supabase users:            {len(supabase_emails)}")
    print(f"")
    print(f"Missing from Supabase (total):   {len(missing_rows)}")
    print(f"  → No school link:              {len(missing_no_school)}")
    print(f"  → Has school link:             {len(missing_with_school)}")

    if missing_no_school:
        print("\n--- Missing contacts WITHOUT school link ---")
        for r in missing_no_school:
            print(f"  {r['email']:<45} role={r['role'] or '(none)':<15} name={r['name']}")

    if missing_with_school:
        print("\n--- Missing contacts WITH school link ---")
        for r in missing_with_school:
            print(f"  {r['email']:<45} role={r['role'] or '(none)':<15} school={r['school_names']}")

    # Role distribution of missing contacts
    from collections import Counter
    role_counts = Counter(r["role"] or "(none)" for r in missing_rows)
    print("\n--- Role distribution of missing contacts ---")
    for role, count in role_counts.most_common():
        print(f"  {role:<30} {count}")


if __name__ == "__main__":
    main()
