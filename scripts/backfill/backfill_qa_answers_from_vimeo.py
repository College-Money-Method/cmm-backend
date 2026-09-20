#!/usr/bin/env python3
"""Recover spoken Q&A answers for webinars the video pipeline never processed.

A question Zoom marks "live answered" carries no answer text anywhere in Zoom —
the answer exists only in the recording. Extraction normally runs off the back
of a video pipeline publish, using the transcript that pipeline writes to S3.
Almost every past webinar predates the pipeline, so almost every live-answered
question is sitting there with no answer at all.

Those replays are on Vimeo, and Vimeo has auto-generated English captions for
nearly all of them. This walks the webinars that still have unanswered spoken
questions, reads their captions, and runs the same matching the pipeline path
runs. The rows it writes carry no ``video_job_id`` — there is no job — and skip
the causality check, which needs a recording clock this source does not have.

Auto-generated captions have no speaker labels, so ``answered_by`` stays empty
on these rows. The answer text is what matters and is unaffected.

Re-running is safe and is the intended way to use this. A webinar whose live
questions already have a verdict is skipped, so an interrupted run resumes where
it stopped, and extraction only ever appends — an admin's edited answer is never
touched. Webinars that do have a published video job are left to that path,
which has the better transcript; ``--include-pipeline`` overrides that.

Costs one Bedrock call per webinar (the whole transcript goes in one prompt) and
two Vimeo calls. Vimeo allows 50 requests/minute across all endpoints, hence the
pacing default.

Usage (from project root):
  uv run python scripts/backfill/backfill_qa_answers_from_vimeo.py --dry-run
  uv run python scripts/backfill/backfill_qa_answers_from_vimeo.py --env-file .env.prod --limit 3
  uv run python scripts/backfill/backfill_qa_answers_from_vimeo.py --env-file .env.prod
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

# Newest first — the recent webinars are the ones an admin is most likely to be
# looking at, so a run that is cut short still covered those.
#
# "Pending" is a live-answered question with no verdict yet. A `failed` row does
# not count as a verdict: those are exactly the "No published video job" rows a
# previous backfill wrote, and re-answering them is the point of this script.
# A question an admin already answered by hand is settled and excluded.
_CANDIDATES_SQL = """
    select w.id,
           w.webinar_name,
           w.start_datetime,
           w.video_embed_code is not null and trim(w.video_embed_code) <> ''
               as has_replay,
           (select count(*) from webinar_video_jobs j
             where j.webinar_id = w.id
               and j.state = 'published'
               and j.frames_prefix is not null) as published_jobs,
           count(*) as pending_questions
      from webinars w
      join webinar_qa_questions q on q.webinar_id = w.id
     where q.answer_source = 'live'
       and q.classification = 'question'
       and coalesce(trim(q.answer_text_override), '') = ''
       and not exists (
             select 1 from webinar_qa_answer_extractions e
              where e.question_id = q.id
                and e.status <> 'failed'
           )
     group by w.id
     order by w.start_datetime desc
"""


def _candidates(db: Session) -> list:
    return list(db.execute(text(_CANDIDATES_SQL)).mappings())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="list webinars, call nothing")
    parser.add_argument("--env-file", default=".env", help="dotenv file to load first")
    parser.add_argument("--limit", type=int, default=0, help="stop after N webinars (0 = all)")
    parser.add_argument(
        "--include-pipeline",
        action="store_true",
        help="also use Vimeo captions for webinars that have a published video job",
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=3.0,
        help="seconds between webinars; Vimeo allows 50 requests/min (default: 3.0)",
    )
    args = parser.parse_args()

    # Load the target environment before importing anything that reads settings.
    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=True)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    # The barrel import is load-bearing, not tidiness: the services below pull in
    # only the models they name, and `Workshop` carries a relationship to `Asset`
    # declared by string. Without every model registered first, the mapper cannot
    # resolve that name and the first query dies.
    import src.db.models  # noqa: F401
    from src.db.base import get_engine
    from src.workshops.models import Webinar
    from src.workshops.qa_extraction_service import extract_answers_from_cues
    from src.workshops.qa_vimeo_transcript import TranscriptUnavailable, load_cues

    engine = get_engine()
    with Session(engine) as db:
        rows = _candidates(db)
        if not args.include_pipeline:
            rows = [r for r in rows if not r["published_jobs"]]
        skipped_no_replay = [r for r in rows if not r["has_replay"]]
        rows = [r for r in rows if r["has_replay"]]
        if args.limit:
            rows = rows[: args.limit]

        pending = sum(r["pending_questions"] for r in rows)
        print(f"{len(rows)} webinar(s) with {pending} unanswered spoken question(s)\n")
        if skipped_no_replay:
            print(f"  ({len(skipped_no_replay)} skipped — no replay video to read captions from)\n")
        if args.dry_run:
            for r in rows:
                print(
                    f"  {r['start_datetime']:%Y-%m-%d}  {r['pending_questions']:>3} q   "
                    f"{(r['webinar_name'] or '')[:50]}"
                )
            return 0

        filled = no_transcript = errors = written_total = 0

        for i, r in enumerate(rows, 1):
            label = f"{r['start_datetime']:%Y-%m-%d} {(r['webinar_name'] or '')[:40]}"
            webinar = db.get(Webinar, r["id"])
            try:
                cues = load_cues(webinar)
            except TranscriptUnavailable as exc:
                # Not an error: a replay without English captions simply cannot
                # be filled from here, and saying so is the useful outcome.
                no_transcript += 1
                print(f"  [{i}/{len(rows)}] {label}: no transcript — {exc}")
                time.sleep(args.pace)
                continue

            try:
                written = extract_answers_from_cues(db, r["id"], cues)
            except Exception as exc:
                # One webinar that blows up must not end the run; the rest are
                # independent of it.
                errors += 1
                db.rollback()
                print(f"  [{i}/{len(rows)}] {label}: error — {exc}")
                time.sleep(args.pace)
                continue

            filled += 1
            written_total += written
            print(f"  [{i}/{len(rows)}] {label}: {written} question(s) run over {len(cues)} cues")
            time.sleep(args.pace)

        print(
            f"\nwebinars filled={filled}  no transcript={no_transcript}  errors={errors}  "
            f"questions run={written_total}"
        )
        print(
            "A question the model found no answer for is stored as `not_found`, not as a "
            "failure — it is a verdict, and it stops the next run re-asking."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
