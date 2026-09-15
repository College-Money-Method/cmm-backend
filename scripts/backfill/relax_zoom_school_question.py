#!/usr/bin/env python3
"""Stop Zoom refusing registrations over the school dropdown, on upcoming webinars.

Parents never see Zoom's registration form — they register on the CMM site and
we push the result through the API. So ``required`` on a Zoom question protects
nothing; it only decides whether Zoom refuses a push that omits an answer.

And we omit constantly. The school question is a ``single_dropdown`` whose
``answers`` list is a fixed snapshot, while schools keep being added: a parent
from a school missing off that list produces no matching answer, the question is
dropped from the payload, and a required question then takes the whole
registration down with it. The parent's row commits, Zoom issues no join link,
and nothing surfaces. That was 233 of 282 failures over thirty days.

Relaxing the flag makes the mismatch harmless — Zoom's copy loses the school,
our ``workshop_registrations.school_id`` keeps it, and that is the copy the host
reads in the admin table. Most upcoming webinars are already configured this
way; this brings the stragglers in line rather than inventing a new rule.

Grade is deliberately left alone: its four answers cover every value in the
data, so it refuses nothing.

Usage (from project root):
  uv run python scripts/backfill/relax_zoom_school_question.py --env-file .env.prod --dry-run
  uv run python scripts/backfill/relax_zoom_school_question.py --env-file .env.prod
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

_UPCOMING_SQL = text("""
    select distinct w.zoom_webinar_id, w.webinar_name
    from webinars w
    where w.zoom_webinar_id is not null
      and w.start_datetime > now()
    order by w.zoom_webinar_id
""")


def _is_school_question(question: dict) -> bool:
    return "school" in str(question.get("title", "")).lower()


def _writable(question: dict) -> dict:
    """A question as Zoom will accept it back.

    Reads and writes are not symmetric: Zoom returns ``answers: []`` on a
    free-text question but refuses that field on write with
    ``custom_questions[N].answers: Invalid field``. So the empty list has to go
    before the question can be echoed back.
    """
    return {k: v for k, v in question.items() if k != "answers" or v}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would change")
    parser.add_argument("--env-file", default=".env", help="dotenv file to load first")
    parser.add_argument("--pace", type=float, default=0.2, help="seconds between Zoom calls")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=True)

    from src.db.base import get_engine
    from src.integrations.zoom import _ZOOM_API_BASE, _get_access_token, _zoom_refusal

    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    engine = get_engine()
    with Session(engine) as db:
        webinars = list(db.execute(_UPCOMING_SQL).mappings())

    print(f"{len(webinars)} upcoming webinar(s) wired to Zoom")
    relaxed = already_ok = skipped = failed = 0

    for w in webinars:
        zoom_webinar_id = w["zoom_webinar_id"]
        name = (w["webinar_name"] or "")[:45]
        url = f"{_ZOOM_API_BASE}/webinars/{zoom_webinar_id}/registrants/questions"

        resp = httpx.get(url, headers=headers, timeout=15.0)
        if resp.is_error:
            print(f"  {zoom_webinar_id} {name}: read failed — {_zoom_refusal(resp)}")
            failed += 1
            continue

        config = resp.json()
        custom = config.get("custom_questions") or []
        school = next((q for q in custom if _is_school_question(q)), None)
        if school is None:
            skipped += 1
            continue
        if not school.get("required"):
            already_ok += 1
            continue

        if args.dry_run:
            print(f"  {zoom_webinar_id} {name}: would relax ({len(school.get('answers') or [])} answers)")
            relaxed += 1
            continue

        # Zoom replaces the whole question set on write, so the untouched
        # questions have to travel back with the one that changed.
        school["required"] = False
        patch = httpx.patch(
            url,
            headers=headers,
            json={
                "questions": config.get("questions") or [],
                "custom_questions": [_writable(q) for q in custom],
            },
            timeout=15.0,
        )
        if patch.is_error:
            print(f"  {zoom_webinar_id} {name}: PATCH failed — {_zoom_refusal(patch)}")
            failed += 1
        else:
            print(f"  {zoom_webinar_id} {name}: relaxed")
            relaxed += 1
        time.sleep(args.pace)

    verb = "would relax" if args.dry_run else "relaxed"
    print(
        f"\n{verb}={relaxed}  already optional={already_ok}  "
        f"no school question={skipped}  failed={failed}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
