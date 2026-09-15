#!/usr/bin/env python3
"""Clear ``required`` on Zoom's registration questions for upcoming webinars.

The registration endpoint now relaxes a webinar's questions itself the first
time Zoom refuses a push over them, so this is not needed to keep registrations
flowing. It exists to do that sweep ahead of time rather than on the back of one
parent's failed registration — the self-healing path still costs that first
parent a retry, and the sweep costs them nothing.

Why relaxing is safe: parents never see Zoom's registration form. They register
on the CMM site and we push the result through the API, so ``required`` guards
nothing and only decides whether Zoom refuses a push that omits an answer. We
omit constantly, because a dropdown's ``answers`` list is a fixed snapshot while
schools keep being added. Zoom's copy loses an unmatched answer either way; our
own tables keep it, and that is the copy the workshop host reads.

Usage (from project root):
  uv run python scripts/backfill/relax_zoom_registration_questions.py --env-file .env.prod --dry-run
  uv run python scripts/backfill/relax_zoom_registration_questions.py --env-file .env.prod
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would change")
    parser.add_argument("--env-file", default=".env", help="dotenv file to load first")
    parser.add_argument("--pace", type=float, default=0.2, help="seconds between Zoom calls")
    args = parser.parse_args()

    from dotenv import load_dotenv

    load_dotenv(args.env_file, override=True)

    from src.db.base import get_engine
    from src.integrations.zoom import (
        _ZOOM_API_BASE,
        _get_access_token,
        _relax_custom_questions,
        _zoom_refusal,
    )

    token = _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    engine = get_engine()
    with Session(engine) as db:
        webinars = list(db.execute(_UPCOMING_SQL).mappings())

    print(f"{len(webinars)} upcoming webinar(s) wired to Zoom")
    relaxed = already_ok = failed = 0

    for w in webinars:
        zoom_webinar_id = w["zoom_webinar_id"]
        name = (w["webinar_name"] or "")[:45]

        if args.dry_run:
            url = f"{_ZOOM_API_BASE}/webinars/{zoom_webinar_id}/registrants/questions"
            resp = httpx.get(url, headers=headers, timeout=15.0)
            if resp.is_error:
                print(f"  {zoom_webinar_id} {name}: read failed — {_zoom_refusal(resp)}")
                failed += 1
                continue
            required = [
                q["title"]
                for q in (resp.json().get("custom_questions") or [])
                if q.get("required")
            ]
            if required:
                print(f"  {zoom_webinar_id} {name}: would relax {len(required)} — {required}")
                relaxed += 1
            else:
                already_ok += 1
            continue

        if _relax_custom_questions(zoom_webinar_id, token):
            print(f"  {zoom_webinar_id} {name}: relaxed")
            relaxed += 1
        else:
            already_ok += 1
        time.sleep(args.pace)

    verb = "would relax" if args.dry_run else "relaxed"
    print(f"\n{verb}={relaxed}  already optional={already_ok}  failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
