"""Copy webinar Q&A answer extractions from an upstream database into a local one.

Local carries the same questions as production — the Zoom Q&A sync runs against
both — but not the recovered answers, because extraction reads a replay
transcript and costs a model call per webinar. Re-running it locally would spend
that again for a result production already has. So this pulls the verdicts down.

The two databases do NOT share primary keys: local was seeded separately and its
webinars and questions carry their own UUIDs. What they do share is Zoom's own
identifiers, and `zoom_question_id` is unique across the table, so that is the
join used here. An extraction whose question is missing locally is skipped
rather than invented.

Extraction rows keep their upstream `id`, which makes the copy idempotent: a
second run inserts nothing. `video_job_id` is a foreign key into a table whose
rows are not synced, so it is kept only when that job happens to exist locally
and nulled otherwise — the column is nullable precisely because losing the job
should not cost us the answer.

Read-only upstream. The target must be a local host; anything else is refused.

    python scripts/db_ops/sync_qa_extractions_prod_to_local.py --dry-run
    python scripts/db_ops/sync_qa_extractions_prod_to_local.py --limit 5
"""

from __future__ import annotations

import argparse
import sys
from urllib.parse import urlparse

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

# A mistyped --to is the one failure that would be expensive, so the target is
# checked against the hosts a developer machine actually serves Postgres on.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}

COLUMNS = (
    "id",
    "question_id",
    "video_job_id",
    "transcript_start_seconds",
    "transcript_end_seconds",
    "answered_by",
    "answer_text",
    "transcript_excerpt",
    "confidence",
    "status",
    "model_id",
    "prompt_version",
    "input_tokens",
    "output_tokens",
    "created_at",
)

# Newest webinar first, so --limit takes the sessions a developer is most likely
# to be looking at rather than an arbitrary slice.
SOURCE_SQL = text(
    """
    select x.*, q.zoom_question_id, w.id as src_webinar_id, w.start_datetime
    from webinar_qa_answer_extractions x
    join webinar_qa_questions q on q.id = x.question_id
    join webinars w on w.id = q.webinar_id
    order by w.start_datetime desc nulls last, x.created_at
    """
)


def engine_for(env_file: str) -> tuple[Engine, str]:
    values = dotenv_values(env_file)
    url = values.get("DATABASE_URL")
    if not url:
        sys.exit(f"{env_file} has no DATABASE_URL")
    return create_engine(url), (urlparse(url).hostname or "")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="source", default=".env.prod", help="Env file to read from")
    parser.add_argument("--to", dest="target", default=".env.local", help="Env file to write to")
    parser.add_argument("--limit", type=int, help="Only the N most recent webinars")
    parser.add_argument("--dry-run", action="store_true", help="Report what would be copied")
    args = parser.parse_args()

    source, _ = engine_for(args.source)
    target, target_host = engine_for(args.target)
    if target_host not in LOCAL_HOSTS:
        sys.exit(f"Refusing to write to '{target_host}' — --to must point at a local database")

    with source.connect() as src:
        rows = [dict(r) for r in src.execute(SOURCE_SQL).mappings().all()]

    if args.limit:
        keep: set = set()
        for row in rows:
            if row["src_webinar_id"] not in keep and len(keep) < args.limit:
                keep.add(row["src_webinar_id"])
        rows = [r for r in rows if r["src_webinar_id"] in keep]

    with target.connect() as dst:
        questions = {
            r[0]: r[1]
            for r in dst.execute(
                text("select zoom_question_id, id from webinar_qa_questions")
            ).all()
        }
        jobs = {r[0] for r in dst.execute(text("select id from webinar_video_jobs")).all()}
        present = {r[0] for r in dst.execute(
            text("select id from webinar_qa_answer_extractions")
        ).all()}

    payload, orphaned, already = [], 0, 0
    for row in rows:
        question_id = questions.get(row["zoom_question_id"])
        if question_id is None:
            orphaned += 1
            continue
        if row["id"] in present:
            already += 1
            continue
        record = {c: row[c] for c in COLUMNS}
        record["question_id"] = question_id
        record["video_job_id"] = row["video_job_id"] if row["video_job_id"] in jobs else None
        payload.append(record)

    webinars = len({r["src_webinar_id"] for r in rows})
    print(f"{len(rows)} upstream extraction(s) across {webinars} webinar(s)")
    print(f"  {len(payload)} to copy, {already} already local, {orphaned} with no matching question")

    if args.dry_run or not payload:
        return 0

    insert = text(
        f"insert into webinar_qa_answer_extractions ({', '.join(COLUMNS)}) "
        f"values ({', '.join(':' + c for c in COLUMNS)})"
    )
    with target.begin() as dst:
        dst.execute(insert, payload)
    print(f"copied {len(payload)} extraction(s) into {target_host}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
