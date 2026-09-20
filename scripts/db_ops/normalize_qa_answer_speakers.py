"""Bring stored `answered_by` values in line with `qa_speaker_names`.

Extractions written before the model was given a spelled roster recorded the
host as whatever the captions made of him — "Paul Merlin", or just "Paul". The
rules that prevent that now live in ``canonical_speaker``; this applies the same
rules to the rows already stored, so the screen does not show one person under
three names.

Nothing is guessed. A row is rewritten only when its name is literally a known
alias or matches a roster entry once case and punctuation are set aside, which
is why a genuinely different person whose name resembles the host's is left
exactly as it was found.

    python scripts/db_ops/normalize_qa_answer_speakers.py --dry-run
    python scripts/db_ops/normalize_qa_answer_speakers.py --env-file .env.prod
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict

from dotenv import dotenv_values
from sqlalchemy import create_engine, text

from src.workshops.qa_speaker_names import DEFAULT_SPEAKER, canonical_speaker

ROWS_SQL = text(
    """
    select x.id, x.answered_by, q.webinar_id
    from webinar_qa_answer_extractions x
    join webinar_qa_questions q on q.id = x.question_id
    where coalesce(trim(x.answered_by), '') <> ''
    """
)

ROSTER_SQL = text(
    """
    select webinar_id, responder_name
    from webinar_qa_questions
    where coalesce(trim(responder_name), '') <> ''
    """
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env.local", help="Which database to repair")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change")
    args = parser.parse_args()

    url = dotenv_values(args.env_file).get("DATABASE_URL")
    if not url:
        sys.exit(f"{args.env_file} has no DATABASE_URL")
    engine = create_engine(url)

    with engine.connect() as conn:
        rosters = defaultdict(lambda: [DEFAULT_SPEAKER])
        for webinar_id, name in conn.execute(ROSTER_SQL).all():
            if name.strip() != DEFAULT_SPEAKER:
                rosters[webinar_id].append(name.strip())
        rows = conn.execute(ROWS_SQL).all()

    changes = []
    for row_id, stored, webinar_id in rows:
        wanted = canonical_speaker(stored, rosters[webinar_id])
        if wanted != stored:
            changes.append({"row_id": row_id, "name": wanted, "was": stored})

    print(f"{len(rows)} row(s) with a speaker; {len(changes)} to correct")
    tally: dict[tuple[str, str], int] = defaultdict(int)
    for change in changes:
        tally[(change["was"], change["name"])] += 1
    for (was, now), count in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {count:4}  {was!r} -> {now!r}")

    if args.dry_run or not changes:
        return 0

    with engine.begin() as conn:
        conn.execute(
            text("update webinar_qa_answer_extractions set answered_by = :name where id = :row_id"),
            [{"row_id": c["row_id"], "name": c["name"]} for c in changes],
        )
    print(f"corrected {len(changes)} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
