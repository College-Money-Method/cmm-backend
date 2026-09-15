#!/usr/bin/env python3
"""Re-push registrations that never reached Zoom, for upcoming webinars only.

``register_webinar`` is deliberately non-fatal: the registration row commits
first, and a Zoom rejection only produces a WARNING. So when Zoom started
rejecting registrations, parents saw a success screen, got an approved row, and
never received a join link — nothing surfaced. This re-sends those rows.

Only future webinars are touched. A join link for a session that already ran is
worthless, and re-registering past attendees would email them about it.

Usage (from project root):
  uv run python scripts/backfill/backfill_missing_zoom_registrants.py --dry-run
  uv run python scripts/backfill/backfill_missing_zoom_registrants.py --env-file .env.prod
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

# Rows to re-send: no Zoom registrant, on a webinar that has not happened yet
# and that is actually wired to a Zoom webinar.
_STRANDED_SQL = text("""
    select r.id, r.email, r.first_name, r.last_name, r.grade, r.questions,
           w.zoom_webinar_id, w.webinar_name, s.name as school_name
    from workshop_registrations r
    join webinars w on w.id = r.webinar_id
    left join schools s on s.id = r.school_id
    where r.zoom_registrant_id is null
      and w.zoom_webinar_id is not null
      and w.start_datetime > now()
    order by w.start_datetime, r.created_at
""")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list rows, call nothing")
    parser.add_argument("--env-file", default=".env", help="dotenv file to load first")
    parser.add_argument("--limit", type=int, default=0, help="stop after N rows (0 = all)")
    parser.add_argument(
        "--pace",
        type=float,
        default=0.2,
        help="seconds between Zoom calls, to stay under the rate limit",
    )
    args = parser.parse_args()

    # Load the target environment before importing anything that reads settings.
    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=True)

    from src.db.base import get_engine
    from src.integrations import zoom as zoom_client

    engine = get_engine()
    with Session(engine) as db:
        rows = list(db.execute(_STRANDED_SQL).mappings())
        if args.limit:
            rows = rows[: args.limit]

        print(f"{len(rows)} stranded registration(s) on upcoming webinars")
        if args.dry_run:
            for r in rows:
                qlen = len(r["questions"] or "")
                print(f"  {r['zoom_webinar_id']}  qlen={qlen:<4} {r['webinar_name'][:45]}")
            return 0

        fixed = still_failing = 0
        for i, r in enumerate(rows, 1):
            registrant_id = zoom_client.register_webinar(
                zoom_webinar_id=r["zoom_webinar_id"],
                email=r["email"],
                first_name=r["first_name"],
                last_name=r["last_name"],
                grade=r["grade"],
                school=r["school_name"],
                questions=r["questions"],
            )
            if registrant_id:
                db.execute(
                    text(
                        "update workshop_registrations set zoom_registrant_id = :rid "
                        "where id = :id"
                    ),
                    {"rid": registrant_id, "id": r["id"]},
                )
                db.commit()
                fixed += 1
            else:
                still_failing += 1
            if i % 25 == 0 or i == len(rows):
                print(f"  {i}/{len(rows)} — recovered={fixed} still failing={still_failing}")
            time.sleep(args.pace)

        print(f"\nrecovered={fixed}  still failing={still_failing}  of {len(rows)}")
        if still_failing:
            print("Still failing means Zoom rejected it again — see the WARNING lines above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
