#!/usr/bin/env python3
"""Pull the Zoom Q&A report for webinars that ended before the sync existed.

Going forward `webinar.ended` fetches the Q&A report itself. Every webinar that
ran before that shipped has questions sitting in Zoom and nothing in our tables.
This walks backwards through them and runs the same sync the webhook runs.

**How far back this reaches is decided by Zoom, not by us**, and Zoom's own
numbers disagree: the web reports UI is documented as keeping a year, while the
Reports API this calls documents six months for the sibling participant report.
Rather than pick one, the default asks for every past webinar and lets Zoom draw
the line itself. Nothing is lost by asking too far back — a webinar past the
window answers 404, which costs one call and is recorded as such.

So the run works newest-first and prints the oldest webinar that actually
returned a report. That printed line is the real boundary, measured rather than
taken on faith. ``--months N`` narrows the range once you know it.

Re-running is safe and is the intended way to use this. A webinar whose report
already synced is skipped, so an interrupted run resumes where it stopped, and
the underlying sync writes ingested fact only — an admin's edited answer
survives a re-sync untouched.

Answer extraction runs only for webinars that have a published video job. Those
are the only ones with a transcript to recover a spoken answer from, and asking
for the rest would write a failure row per question saying so.

Usage (from project root):
  uv run python scripts/backfill/backfill_webinar_qa.py --dry-run
  uv run python scripts/backfill/backfill_webinar_qa.py --env-file .env.prod --limit 5
  uv run python scripts/backfill/backfill_webinar_qa.py --env-file .env.prod
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

# Newest first: the far end of the range is where Zoom stops answering, so
# starting at the recent end means a run that is cut short still got the
# webinars most likely to have a report.
#
# `ok_syncs` is what makes a re-run cheap, and `published_jobs` is checked here
# rather than inside the extraction so a webinar with no recording never gets
# asked for a transcript it does not have.
_CANDIDATES_SQL = """
    select w.id,
           w.zoom_webinar_id,
           w.webinar_name,
           w.start_datetime,
           (select count(*) from webinar_qa_syncs s
             where s.webinar_id = w.id and s.status = 'ok') as ok_syncs,
           (select count(*) from webinar_video_jobs j
             where j.webinar_id = w.id
               and j.state = 'published'
               and j.frames_prefix is not null) as published_jobs
      from webinars w
     where w.zoom_webinar_id is not null
       and w.start_datetime < now()
       {window}
     order by w.start_datetime desc
"""


def _candidates(db: Session, months: int) -> list:
    window = (
        "and w.start_datetime > now() - make_interval(months => :months)" if months else ""
    )
    sql = text(_CANDIDATES_SQL.format(window=window))
    params = {"months": months} if months else {}
    return list(db.execute(sql, params).mappings())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list webinars, call nothing")
    parser.add_argument("--env-file", default=".env", help="dotenv file to load first")
    parser.add_argument(
        "--months",
        type=int,
        default=0,
        help="narrow to the last N months; 0 means every past webinar (default: 0)",
    )
    parser.add_argument("--limit", type=int, default=0, help="stop after N webinars (0 = all)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-sync webinars that already have a successful sync",
    )
    parser.add_argument(
        "--no-extract",
        action="store_true",
        help="skip recovering live-answered answers from the transcript",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=1.0,
        help="seconds between webinars; the Reports API is rate limited (default: 1.0)",
    )
    args = parser.parse_args()

    # Load the target environment before importing anything that reads settings.
    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=True)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # The barrel import is load-bearing, not tidiness: the sync only pulls in the
    # handful of models it names, and `Workshop` carries a relationship to
    # `Asset` declared by string. Without every model registered first, the
    # mapper cannot resolve that name and the first query dies.
    import src.db.models  # noqa: F401
    from src.db.base import get_engine
    from src.workshops.qa_extraction_service import extract_answers
    from src.workshops.qa_sync_service import sync_webinar_qa

    engine = get_engine()
    with Session(engine) as db:
        rows = _candidates(db, args.months)
        if not args.force:
            rows = [r for r in rows if not r["ok_syncs"]]
        if args.limit:
            rows = rows[: args.limit]

        scope = f"last {args.months} month(s)" if args.months else "all time"
        print(f"{len(rows)} webinar(s) to back-fill — {scope}\n")
        if args.dry_run:
            for r in rows:
                video = "video" if r["published_jobs"] else "no video"
                print(
                    f"  {r['start_datetime']:%Y-%m-%d}  {r['zoom_webinar_id']:<12} "
                    f"{video:<8} {(r['webinar_name'] or '')[:45]}"
                )
            return 0

        synced = unavailable = errors = extracted = 0
        oldest_with_report = None

        for i, r in enumerate(rows, 1):
            label = f"{r['start_datetime']:%Y-%m-%d} {(r['webinar_name'] or '')[:40]}"
            try:
                ok = sync_webinar_qa(r["zoom_webinar_id"], db)
            except Exception as exc:
                # One webinar that blows up must not end the run — the next one
                # is independent, and the remaining range is the part that still
                # has reports worth fetching.
                #
                # Counted apart from `unavailable`: a crash on our side is not
                # Zoom declining to answer, and folding the two together would
                # have the summary report a retention boundary that never
                # happened.
                errors += 1
                db.rollback()
                print(f"  [{i}/{len(rows)}] {label}: error — {exc}")
                continue

            if not ok:
                unavailable += 1
                print(f"  [{i}/{len(rows)}] {label}: no report")
            else:
                synced += 1
                oldest_with_report = r["start_datetime"]
                note = ""
                if r["published_jobs"] and not args.no_extract:
                    written = extract_answers(db, r["id"])
                    extracted += written
                    note = f", {written} live answer(s) extracted" if written else ""
                print(f"  [{i}/{len(rows)}] {label}: synced{note}")

            time.sleep(args.pace)

        print(
            f"\nsynced={synced}  no report={unavailable}  errors={errors}  "
            f"answers extracted={extracted}"
        )
        if oldest_with_report:
            print(f"Oldest webinar Zoom still had a report for: {oldest_with_report:%Y-%m-%d}")
        if errors:
            print(f"{errors} webinar(s) failed on our side, not Zoom's — see the errors above.")
        if unavailable:
            print(
                "'no report' is Zoom answering 404 — either outside its retention window "
                "or a webinar that never had a Q&A panel. Each one left a failed row in "
                "webinar_qa_syncs saying so."
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
