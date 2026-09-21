"""Re-label stored Q&A submissions with the current classification prompt.

Classification runs once, inside the Zoom sync, and only ever fills a label that
is null — which is right for ingestion and useless after the prompt improves.
The rows that were labelled under the old wording keep the old verdict forever,
so a fix to the prompt reaches new webinars and leaves the archive wrong.

This re-asks the model about rows already labelled. By default only the ones
currently labelled as noise, because that is the direction the damage runs: a
polite opener ("Sorry, just joined. Is the webinar recorded?") buried a real
question under 'greeting', and a row already labelled 'question' cannot be
improved by asking again, only demoted by ordinary model variance.

Three things it will not do:

* **Touch an admin's override.** A row with ``classification_override`` set, or
  labelled ``admin``, is somebody's explicit decision and is skipped outright.
* **Leave a row worse than it found it.** A chunk the model fails on would null
  out perfectly good labels, so the old label is put back for any row that came
  back unlabelled.
* **Write half a batch.** Everything happens inside one transaction that
  ``--dry-run`` rolls back, so a dry run shows the real verdicts — at the real
  model cost — without changing a label.

    python scripts/db_ops/reclassify_qa_questions.py --dry-run
    python scripts/db_ops/reclassify_qa_questions.py --env-file .env.prod
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import defaultdict

from dotenv import dotenv_values

# Question bodies reach the terminal from here. Attendees paste their address
# into the question itself ("email me at ..."), so the sample lines are masked
# rather than trusted to be free of it.
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SAMPLE_WIDTH = 100


def _load_env(path: str) -> str:
    """Put the target environment in place before anything reads settings.

    Both the database and the Bedrock credentials come from here. It matters
    that this happens before ``src.config`` is imported: settings are built once
    at import, and the usage ledger opens its own session from them — point the
    script at prod with a stale environment and the spend lands in another
    database.
    """
    values = dotenv_values(path)
    url = values.get("DATABASE_URL")
    if not url:
        sys.exit(f"{path} has no DATABASE_URL")
    for key, value in values.items():
        if value is not None:
            os.environ[key] = value
    return url


def _sample(text: str) -> str:
    masked = _EMAIL.sub("[email]", " ".join(text.split()))
    return masked[:SAMPLE_WIDTH] + ("…" if len(masked) > SAMPLE_WIDTH else "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env.local", help="Which database to re-label")
    parser.add_argument(
        "--dry-run", action="store_true", help="Ask the model, report, roll back"
    )
    parser.add_argument(
        "--include-questions",
        action="store_true",
        help="Also re-ask about rows already labelled 'question'",
    )
    args = parser.parse_args()

    url = _load_env(args.env_file)

    # Imported here, after the environment is in place, for the reason given in
    # _load_env. The barrel registers every model so the mappers configure.
    import src.db.models  # noqa: F401, PLC0415
    from sqlalchemy import create_engine, select  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415

    from src.workshops.qa_classification_service import (  # noqa: PLC0415
        NOISE_PROMPT_VERSION,
        classify_questions,
    )
    from src.workshops.qa_models import WebinarQaQuestion  # noqa: PLC0415

    engine = create_engine(url)
    query = (
        select(WebinarQaQuestion)
        .where(
            WebinarQaQuestion.classification.is_not(None),
            WebinarQaQuestion.classification_override.is_(None),
            WebinarQaQuestion.classified_by != "admin",
        )
        .order_by(WebinarQaQuestion.asked_at)
    )
    if not args.include_questions:
        query = query.where(WebinarQaQuestion.classification != "question")

    connection = engine.connect()
    outer = connection.begin()
    # The service commits, as it must when the sync calls it. Joining its
    # session to this transaction as a savepoint means those commits stay inside
    # the transaction this script controls, so --dry-run really does undo them.
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        rows = list(db.scalars(query).all())
        if not rows:
            print("nothing to re-label")
            return 0

        before = {row.id: row.classification for row in rows}
        for row in rows:
            row.classification = None

        print(f"re-asking about {len(rows)} row(s) under prompt {NOISE_PROMPT_VERSION}…")
        classify_questions(db, rows)

        # A chunk the model failed on leaves its rows null, which would drop the
        # old verdict on the floor. Put it back: an unchanged label is a far
        # better outcome than a lost one.
        restored = 0
        for row in rows:
            if row.classification is None:
                row.classification = before[row.id]
                restored += 1
        if restored:
            db.commit()

        changes = [(before[row.id], row.classification, row.question_text) for row in rows]
        _report(changes, restored)

        if args.dry_run:
            outer.rollback()
            print("dry run — nothing written")
        else:
            outer.commit()
            changed = sum(1 for was, now, _ in changes if was != now)
            print(f"wrote {changed} corrected label(s)")
    finally:
        db.close()
        connection.close()
    return 0


def _report(changes: list[tuple[str, str, str]], restored: int) -> None:
    tally: dict[tuple[str, str], int] = defaultdict(int)
    for was, now, _ in changes:
        tally[(was, now)] += 1

    moved = [(was, now, text) for was, now, text in changes if was != now]
    print(f"{len(changes)} re-asked, {len(moved)} changed, {restored} left as found")
    for (was, now), count in sorted(tally.items(), key=lambda kv: -kv[1]):
        mark = "  " if was == now else "->"
        print(f"  {count:4}  {was:>8} {mark} {now}")

    for was, now, text in moved:
        print(f"  {was:>8} -> {now:<8} {_sample(text)}")


if __name__ == "__main__":
    raise SystemExit(main())
