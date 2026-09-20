"""Seed the spend ledger with the Q&A extraction history that survived.

The ledger starts empty, but Q&A extraction is the one caller that already kept
its token counts — on the extraction rows themselves — so its past runs can be
reconstructed. Nothing else can: trim-point tokens were returned to the caller
and dropped, and topic segmentation, Q&A classification and frame
classification only ever logged theirs. Those are gone, and the page will show
them from their next run onward rather than pretending otherwise.

One model call produces one extraction row per question, each carrying the same
token counts and the same transaction timestamp, so a run is the distinct
(webinar, timestamp, tokens) tuple rather than the row count.

Ids are derived from that tuple with uuid5, so running this twice inserts
nothing the second time.

    python scripts/db_ops/backfill_bedrock_usage_from_qa.py --dry-run
    python scripts/db_ops/backfill_bedrock_usage_from_qa.py --env-file .env.prod
"""

from __future__ import annotations

import argparse
import sys
import uuid

from dotenv import dotenv_values
from sqlalchemy import create_engine, text

from src.video_pipeline.bedrock_usage import QA_EXTRACTION, cost_usd

# The namespace makes the derived ids stable across runs and machines.
NAMESPACE = uuid.UUID("6f1a9d2e-9a1e-4a6f-8f7b-2c0d5b3e7a41")

RUNS_SQL = text(
    """
    select distinct q.webinar_id, x.created_at, x.input_tokens, x.output_tokens, x.model_id
    from webinar_qa_answer_extractions x
    join webinar_qa_questions q on q.id = x.question_id
    where x.input_tokens is not null and x.input_tokens > 0
    order by x.created_at
    """
)

INSERT_SQL = text(
    """
    insert into bedrock_usage
        (id, invoke_type, model_id, input_tokens, output_tokens, cost_usd, created_at)
    values (:id, :invoke_type, :model_id, :input_tokens, :output_tokens, :cost_usd, :created_at)
    on conflict (id) do nothing
    """
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=".env.local")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    url = dotenv_values(args.env_file).get("DATABASE_URL")
    if not url:
        sys.exit(f"{args.env_file} has no DATABASE_URL")
    engine = create_engine(url)

    with engine.connect() as conn:
        runs = conn.execute(RUNS_SQL).all()

    rows = []
    for webinar_id, created_at, in_tok, out_tok, model_id in runs:
        key = f"{QA_EXTRACTION}|{webinar_id}|{created_at.isoformat()}|{in_tok}|{out_tok}"
        rows.append(
            {
                "id": uuid.uuid5(NAMESPACE, key),
                "invoke_type": QA_EXTRACTION,
                "model_id": model_id or "unknown",
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cost_usd": cost_usd(in_tok, out_tok),
                "created_at": created_at,
            }
        )

    total = sum(r["cost_usd"] for r in rows)
    print(f"{len(rows)} extraction run(s), ${total:.4f}")
    if args.dry_run or not rows:
        return 0

    with engine.begin() as conn:
        conn.execute(INSERT_SQL, rows)
    with engine.connect() as conn:
        held = conn.execute(text("select count(*) from bedrock_usage")).scalar()
    print(f"ledger now holds {held} row(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
