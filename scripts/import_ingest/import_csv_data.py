"""Import Airtable CSV exports into the Supabase PostgreSQL database.

Usage:
    uv run python scripts/import_csv_data.py              # full import
    uv run python scripts/import_csv_data.py --table cycles  # single table
    uv run python scripts/import_csv_data.py --dry-run     # preview only

Requires DATABASE_URL in .env (Supabase Postgres connection string).
"""

from __future__ import annotations

import argparse
import csv
import sys
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session
import sqlalchemy as sa

# ---------------------------------------------------------------------------
# Bootstrap: ensure project root is on sys.path so `src.*` imports work.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.db.base import Base, get_engine  # noqa: E402
from src.db.models import (  # noqa: E402
    Asset,
    Cohort,
    Contact,
    Cycle,
    Invoice,
    OneOnOneMeeting,
    PaulMartinCalendar,
    PortalMapping,
    Sale,
    SalesStatus,
    SchoolDateSelector,
    School,
    Setting,
    Webinar,
    Workshop,
    WorkshopAsset,
    WorkshopRegistration,
)

CSV_DIR = PROJECT_ROOT / "airtable_csv_exports"

# Airtable record‑ID → new UUID mapping, keyed by table name.
ID_MAP: dict[str, dict[str, uuid.UUID]] = {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_csv(filename: str) -> list[dict[str, str]]:
    path = CSV_DIR / filename
    if not path.exists():
        print(f"  ⚠ {filename} not found, skipping.")
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _register_ids(table: str, rows: list[dict[str, str]]) -> None:
    """Pre-generate a UUID for every Airtable record ID in a table."""
    mapping: dict[str, uuid.UUID] = {}
    for row in rows:
        airtable_id = row.get("id", "").strip()
        if airtable_id:
            mapping[airtable_id] = uuid.uuid4()
    ID_MAP[table] = mapping


def _resolve_id(table: str, airtable_id: str | None) -> uuid.UUID | None:
    """Look up the UUID for an Airtable record ID, or None."""
    if not airtable_id:
        return None
    aid = airtable_id.strip()
    return ID_MAP.get(table, {}).get(aid)


def _first_ref(value: str | None) -> str | None:
    """Extract the first Airtable record ID from a possibly comma-separated field."""
    if not value:
        return None
    first = value.split(",")[0].strip()
    return first if first.startswith("rec") else None


def _bool(value: str | None) -> bool:
    if not value:
        return False
    return value.strip().lower() in ("true", "checked", "1", "yes")


def _int(value: str | None) -> int | None:
    if not value or not value.strip():
        return None
    try:
        return int(float(value.strip()))
    except (ValueError, TypeError):
        return None


def _decimal(value: str | None) -> Decimal | None:
    if not value or not value.strip():
        return None
    try:
        return Decimal(value.strip().replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _date(value: str | None) -> date | None:
    if not value or not value.strip():
        return None
    v = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def _datetime(value: str | None) -> datetime | None:
    if not value or not value.strip():
        return None
    v = value.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
        "%m/%d/%Y %H:%M:%S",
    ):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _text(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    return value.strip()


# ---------------------------------------------------------------------------
# Per-table import functions
# ---------------------------------------------------------------------------


def import_cycles(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["cycles"][aid]
        session.add(Cycle(
            id=uid,
            name=row.get("Name", "").strip(),
            beginning_date=_date(row.get("Beginning Date")),
            end_date=_date(row.get("End Date")),
            is_current=_bool(row.get("Current")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
    session.flush()

    # Second pass: set next/prev cycle by date ordering
    cycles_by_date = sorted(
        [(uid, _date(r.get("Beginning Date"))) for r, uid in zip(rows, [ID_MAP["cycles"][r["id"]] for r in rows]) if _date(r.get("Beginning Date"))],
        key=lambda x: x[1],
    )
    for i, (uid, _) in enumerate(cycles_by_date):
        updates: dict[str, Any] = {}
        if i + 1 < len(cycles_by_date):
            updates["next_cycle_id"] = cycles_by_date[i + 1][0]
        if i > 0:
            updates["prev_cycle_id"] = cycles_by_date[i - 1][0]
        if updates:
            session.execute(
                text("UPDATE cycles SET "
                     + ", ".join(f"{k} = :_{k}" for k in updates)
                     + " WHERE id = :_id"),
                {f"_{k}": v for k, v in updates.items()} | {"_id": uid},
            )
    session.flush()
    return count


def import_cohorts(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["cohorts"][aid]
        session.add(Cohort(
            id=uid,
            name=row.get("Name", "").strip(),
            hide_unavailability_calendar=_bool(row.get("Hide Unavailability Calendar")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
    session.flush()
    return count


def import_workshops(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["workshops"][aid]
        session.add(Workshop(
            id=uid,
            name=row.get("Name", "").strip() or "Untitled",
            description=_text(row.get("Description")),
            key_actions=_text(row.get("Workshop Key Actions")),
            sequence_number=_int(row.get("Webinar Sequence")),
            suggested_grades=_text(row.get("Suggested Grades")),
            resource_center_slug=_text(row.get("Resource Center Slug")),
            workshop_art_url=_text(row.get("Workshop Art")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
    session.flush()
    return count


def import_schools(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["schools"][aid]
        cohort_ref = _first_ref(row.get("Cohort 2"))
        session.add(School(
            id=uid,
            name=row.get("School", "").strip() or "Unknown",
            street_address=_text(row.get("Street Address")),
            city=_text(row.get("City")),
            state=_text(row.get("State")),
            zip_code=_text(row.get("Zip Code")),
            enrollment_9_12=_int(row.get("Enrollment (9-12)")),
            cmm_website_password=_text(row.get("CMM Website Password")),
            slug=_text(row.get("slug")),
            school_resource_center_url=_text(row.get("School Resource Center URL")),
            appointlet_link=_text(row.get("Appointlet Link")),
            calendar_link=_text(row.get("Calendar Link")),
            logo_url=_text(row.get("Logo")),
            is_current_customer=_bool(row.get("Current Customer")),
            cohort_id=_resolve_id("cohorts", cohort_ref),
            bubble_rec_id=_text(row.get("BubbleRecID")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
    session.flush()
    return count


def import_contacts(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    skipped = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["contacts"][aid]
        school_ref = _first_ref(row.get("Sch"))
        school_id = _resolve_id("schools", school_ref)
        if not school_id:
            skipped += 1
            del ID_MAP["contacts"][aid]
            continue
        session.add(Contact(
            id=uid,
            school_id=school_id,
            first_name=_text(row.get("First Name")),
            last_name=_text(row.get("Last Name")),
            email=_text(row.get("Email")),
            role=_text(row.get("Role")),
            magic_link=_text(row.get("Magic Link")),
            receive_comms=_bool(row.get("Receive Comms")),
            auto_emails=_bool(row.get("Auto Emails")),
            softr_access=_bool(row.get("Softr Access")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
    if skipped:
        print(f"    (skipped {skipped} contacts with missing school reference)")
    session.flush()
    return count


def import_webinars(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    skipped = 0
    seen_keys: set[tuple[uuid.UUID, uuid.UUID, uuid.UUID]] = set()

    for row in rows:
        aid = row["id"]
        uid = ID_MAP["webinars"][aid]

        workshop_ref = _first_ref(row.get("Workshops"))
        cohort_ref = _first_ref(row.get("Cohort"))
        cycle_ref = _first_ref(row.get("Cycle"))

        workshop_id = _resolve_id("workshops", workshop_ref)
        cohort_id = _resolve_id("cohorts", cohort_ref)
        cycle_id = _resolve_id("cycles", cycle_ref)

        if not (workshop_id and cohort_id and cycle_id):
            skipped += 1
            del ID_MAP["webinars"][aid]
            continue

        key = (workshop_id, cohort_id, cycle_id)
        if key in seen_keys:
            skipped += 1
            del ID_MAP["webinars"][aid]
            continue
        seen_keys.add(key)

        session.add(Webinar(
            id=uid,
            workshop_id=workshop_id,
            cohort_id=cohort_id,
            cycle_id=cycle_id,
            webinar_name=_text(row.get("Webinar Name")),
            zoom_webinar_id=_text(row.get("Webinar ID")),
            start_datetime=_datetime(row.get("Start Date and Time")),
            end_datetime=_datetime(row.get("End Date and Time")),
            join_url=_text(row.get("JoinURL")),
            start_url=_text(row.get("StartURL")),
            registration_url=_text(row.get("RegistrationURL")),
            zoom_link=_text(row.get("Zoom Link")),
            video_embed_code=_text(row.get("Video Embed Code")),
            audio_transcript=_text(row.get("Audio Transcript")),
            track_registrations=_bool(row.get("Track Registrations")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
    if skipped:
        print(f"    (skipped {skipped} webinars with missing FKs or duplicate keys)")
    session.flush()
    return count


def import_workshop_registrations(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    skipped = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["workshop_registrations"][aid]

        webinar_ref = _first_ref(row.get("Junction Table School Workshop"))
        school_ref = _first_ref(row.get("School"))

        webinar_id = _resolve_id("webinars", webinar_ref)
        school_id = _resolve_id("schools", school_ref)

        email = _text(row.get("Email"))
        if not webinar_id or not email:
            skipped += 1
            continue

        status_raw = _text(row.get("Status")) or "approved"
        status_val = status_raw.lower() if status_raw.lower() in ("approved", "pending", "denied") else "approved"

        session.add(WorkshopRegistration(
            id=uid,
            webinar_id=webinar_id,
            school_id=school_id,
            first_name=_text(row.get("First Name")),
            last_name=_text(row.get("Last Name")),
            email=email,
            grade=_text(row.get("Grade")),
            status=status_val,
            attended=_bool(row.get("Attended")),
            join_time=_datetime(row.get("Join Time")),
            leave_time=_datetime(row.get("Leave Time")),
            zoom_registrant_id=_text(row.get("ZoomRegistrantID")),
            questions=_text(row.get("Questions")),
            registration_time=_datetime(row.get("registration time")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1

        if count % 5000 == 0:
            session.flush()
            print(f"    ... flushed {count} registrations")

    if skipped:
        print(f"    (skipped {skipped} registrations with missing webinar or email)")
    session.flush()
    return count


def import_portal_mapping(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    skipped = 0
    seen_pairs: set[tuple[uuid.UUID, uuid.UUID]] = set()

    for row in rows:
        aid = row["id"]
        uid = ID_MAP["portal_mapping"][aid]

        school_ref = _first_ref(row.get("Schools"))
        webinar_ref = _first_ref(row.get("Webinar"))

        school_id = _resolve_id("schools", school_ref)
        webinar_id = _resolve_id("webinars", webinar_ref)

        if not (school_id and webinar_id):
            skipped += 1
            del ID_MAP["portal_mapping"][aid]
            continue

        pair = (school_id, webinar_id)
        if pair in seen_pairs:
            skipped += 1
            del ID_MAP["portal_mapping"][aid]
            continue
        seen_pairs.add(pair)

        session.add(PortalMapping(
            id=uid,
            school_id=school_id,
            webinar_id=webinar_id,
            pre_webinar_reminder_sent_on=_datetime(row.get("Pre-Webinar Reminder Sent on")),
            post_webinar_update_sent_on=_datetime(row.get("Post-Webinar Update Sent on")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1

        if count % 5000 == 0:
            session.flush()
            print(f"    ... flushed {count} portal mappings")

    if skipped:
        print(f"    (skipped {skipped} portal mappings with missing FKs or duplicates)")
    session.flush()
    return count


def import_sales(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    skipped = 0
    valid_statuses = {s.value for s in SalesStatus}

    for row in rows:
        aid = row["id"]
        uid = ID_MAP["sales"][aid]

        school_ref = _first_ref(row.get("Schools"))
        cycle_ref = _first_ref(row.get("Cycle"))
        signatory_ref = _first_ref(row.get("Contract Signatory"))

        school_id = _resolve_id("schools", school_ref)
        cycle_id = _resolve_id("cycles", cycle_ref)

        if not (school_id and cycle_id):
            skipped += 1
            continue

        raw_status = _text(row.get("Sales Status")) or "Prospect"
        status = raw_status if raw_status in valid_statuses else "Prospect"

        raw_proposal = _text(row.get("Proposal Type"))
        proposal_type = raw_proposal if raw_proposal in ("Fixed", "Variable") else None

        session.add(Sale(
            id=uid,
            school_id=school_id,
            cycle_id=cycle_id,
            contract_signatory_id=_resolve_id("contacts", signatory_ref),
            status=status,
            proposal_type=proposal_type,
            contract_url=_text(row.get("Contract")),
            contract_doc_id=_text(row.get("ContractDocID")),
            proposal_url=_text(row.get("Proposal")),
            proposal_doc_id=_text(row.get("ProposalDocID")),
            contract_signed_date=_date(row.get("Contract Signed")),
            contract_sent_date=_date(row.get("Contract Sent")),
            contract_created_at=_datetime(row.get("Contract created")),
            proposal_sent_date=_date(row.get("Proposal Sent")),
            proposal_accepted=_bool(row.get("Proposal Accepted")),
            proposal_rejected=_bool(row.get("Proposal Rejected")),
            fixed_cost=_decimal(row.get("Fixed Cost")),
            signed_revenue=_decimal(row.get("Signed Revenue")),
            revenue_potential=_decimal(row.get("Revenue Potential")),
            contract_rate=_decimal(row.get("Contract Rate")),
            hours_contracted_1on1=_decimal(row.get("1-1 hours contracted")),
            payments_received=_bool(row.get("Payments Received")),
            enrollment_at_signing=_int(row.get("Enrollment (9-12) (from Schools)")),
            wp_updated=_bool(row.get("WP Updated")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
    if skipped:
        print(f"    (skipped {skipped} sales with missing school or cycle)")
    session.flush()
    return count


def import_assets(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["assets"][aid]
        cycle_ref = _first_ref(row.get("Cycle"))

        session.add(Asset(
            id=uid,
            name=row.get("Name", "").strip() or "Untitled",
            description=_text(row.get("Asset Description")),
            file_link=_text(row.get("File Link")),
            attachment_url=_text(row.get("Attachment")),
            asset_date=_date(row.get("Date")),
            is_active=_bool(row.get("Active")),
            cycle_id=_resolve_id("cycles", cycle_ref),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
    session.flush()

    # Workshop–Assets junction
    junction_count = 0
    for row in rows:
        asset_uid = ID_MAP["assets"][row["id"]]
        workshops_raw = row.get("Workshops", "")
        if not workshops_raw:
            continue
        for wref in workshops_raw.split(","):
            wref = wref.strip()
            workshop_id = _resolve_id("workshops", wref)
            if workshop_id:
                session.add(WorkshopAsset(workshop_id=workshop_id, asset_id=asset_uid))
                junction_count += 1
    if junction_count:
        print(f"    ({junction_count} workshop–asset links)")
    session.flush()
    return count


def import_one_on_one_meetings(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    skipped = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["one_on_one_meetings"][aid]

        school_ref = _first_ref(row.get("School"))
        cycle_ref = _first_ref(row.get("Cycle"))

        session.add(OneOnOneMeeting(
            id=uid,
            school_id=_resolve_id("schools", school_ref),
            cycle_id=_resolve_id("cycles", cycle_ref),
            first_name=_text(row.get("First Name")),
            last_name=_text(row.get("Last Name")),
            email=_text(row.get("Email")),
            grade=_text(row.get("Grade")),
            scheduled_at=_datetime(row.get("Date")),
            status=_text(row.get("Status")),
            meeting_goals=_text(row.get("Meeting Goals")),
            notes=_text(row.get("Notes")),
            college_list=_text(row.get("College List")),
            conference_url=_text(row.get("Conference URL")),
            is_school_sponsored=_bool(row.get("School Sponsored")),
            is_invoiced=_bool(row.get("Invoiced")),
            ai_meeting_summary=_text(row.get("AI Meeting Summaries")),
            reminder_1_sent_on=_datetime(row.get("Reminder 1 Sent On")),
            reminder_2_sent_on=_datetime(row.get("Reminder 2 Sent On")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
    session.flush()
    return count


def import_school_date_selector(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    skipped = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["school_date_selector"][aid]

        school_ref = _first_ref(row.get("Schools"))
        workshop_ref = _first_ref(row.get("Workshops"))
        school_id = _resolve_id("schools", school_ref)
        dt = _date(row.get("Date"))

        if not (school_id and dt):
            skipped += 1
            continue

        session.add(SchoolDateSelector(
            id=uid,
            school_id=school_id,
            workshop_id=_resolve_id("workshops", workshop_ref),
            date=dt,
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1

        if count % 2000 == 0:
            session.flush()

    if skipped:
        print(f"    (skipped {skipped} date selectors with missing school or date)")
    session.flush()
    return count


def import_paul_martin_calendar(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["paul_martin_calendar"][aid]
        session.add(PaulMartinCalendar(
            id=uid,
            title=_text(row.get("Title")),
            start_datetime=_datetime(row.get("Start")),
            end_datetime=_datetime(row.get("End")),
            google_event_id=_text(row.get("Event ID")),
            event_link=_text(row.get("Event Link")),
            hangouts_link=_text(row.get("Hangouts Link")),
            description=_text(row.get("Description")),
            location=_text(row.get("Location")),
            status=_text(row.get("Status")),
            creator=_text(row.get("Creator")),
            is_recurring=_bool(row.get("Recurring Event")),
            is_all_day=_bool(row.get("All Day")),
            google_updated_at=_datetime(row.get("Updated")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
        if count % 5000 == 0:
            session.flush()
    session.flush()
    return count


def import_settings(session: Session, rows: list[dict[str, str]]) -> int:
    count = 0
    for row in rows:
        aid = row["id"]
        uid = ID_MAP["settings"][aid]
        session.add(Setting(
            id=uid,
            name=row.get("Name", "").strip() or "Untitled",
            action=_text(row.get("Action")),
            days_prior=_int(row.get("Days Prior")),
            trigger_url=_text(row.get("Trigger")),
            webhook_url=_text(row.get("Webhook")),
            created_at=_datetime(row.get("createdTime")) or datetime.utcnow(),
        ))
        count += 1
    session.flush()
    return count


# ---------------------------------------------------------------------------
# Import pipeline definition
# ---------------------------------------------------------------------------

TABLES = [
    ("cycles",                    "Cycle.csv",                   import_cycles),
    ("cohorts",                   "Cohort.csv",                  import_cohorts),
    ("workshops",                 "Workshops.csv",               import_workshops),
    ("schools",                   "Schools.csv",                 import_schools),
    ("contacts",                  "Contacts.csv",                import_contacts),
    ("webinars",                  "Webinars.csv",                import_webinars),
    ("workshop_registrations",    "Workshop Registrations.csv",  import_workshop_registrations),
    ("portal_mapping",            "Portal Mapping Table.csv",    import_portal_mapping),
    ("sales",                     "Sales.csv",                   import_sales),
    ("assets",                    "Assets.csv",                  import_assets),
    ("one_on_one_meetings",       "One-on-one Meetings.csv",     import_one_on_one_meetings),
    ("school_date_selector",      "School Date Selector.csv",    import_school_date_selector),
    ("paul_martin_calendar",      "PAUL MARTIN.csv",             import_paul_martin_calendar),
    ("settings",                  "Settings.csv",                import_settings),
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Import Airtable CSV data into Postgres")
    parser.add_argument("--table", help="Import only this table (by DB table name)")
    parser.add_argument("--dry-run", action="store_true", help="Read CSVs and show counts without writing")
    parser.add_argument("--reset", action="store_true", help="Truncate all tables before importing")
    args = parser.parse_args()

    # Phase 1: read all CSVs and build ID maps
    print("Phase 1: Reading CSVs and building ID maps ...")
    csv_data: dict[str, list[dict[str, str]]] = {}
    for table_name, csv_file, _ in TABLES:
        rows = _read_csv(csv_file)
        csv_data[table_name] = rows
        _register_ids(table_name, rows)
        print(f"  {table_name:30s} {len(rows):>8,} rows  ({len(ID_MAP.get(table_name, {})):>8,} IDs)")

    if args.dry_run:
        print("\n--dry-run: no database changes made.")
        return

    # Phase 2: apply migration and import
    print("\nPhase 2: Connecting to database ...")
    engine = get_engine()

    if args.reset:
        print("  Truncating all tables (--reset) ...")
        with engine.connect() as conn:
            table_names = [t[0] for t in TABLES]
            table_names.extend(["workshop_assets", "invoices"])
            conn.execute(sa.text(
                "TRUNCATE " + ", ".join(table_names) + " CASCADE"
            ))
            conn.commit()
        print("  ✓ All tables truncated.")

    with Session(engine) as session:
        for table_name, csv_file, import_fn in TABLES:
            if args.table and args.table != table_name:
                continue
            rows = csv_data[table_name]
            if not rows:
                print(f"  {table_name}: 0 rows (skipped)")
                continue
            print(f"  Importing {table_name} ...")
            try:
                n = import_fn(session, rows)
                print(f"  ✓ {table_name}: {n:,} rows imported")
            except Exception as exc:
                print(f"  ✗ {table_name}: FAILED — {exc}")
                session.rollback()
                raise

        print("\nCommitting transaction ...")
        session.commit()
        print("Done! All data imported successfully.")


if __name__ == "__main__":
    main()
