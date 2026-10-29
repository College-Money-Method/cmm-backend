"""One portable query: each visible college's latest aid record.

Latest = real years beat N/R, then newest year:
``ORDER BY (cds_year IS NULL), cds_year DESC`` (a portable NULLS LAST). A
college with only an N/R record keeps it, with ``cds_year`` null.
"""

from __future__ import annotations

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from src.colleges.models import College, CollegeAidRecord

# Safety cap; the dataset is ~463 colleges and is returned unpaginated.
MAX_ROWS = 2000


def latest_records_query():
    """SELECT (College, CollegeAidRecord) for the latest record of each non-hidden college."""
    rank = (
        func.row_number()
        .over(
            partition_by=CollegeAidRecord.college_id,
            order_by=(
                case((CollegeAidRecord.cds_year.is_(None), 1), else_=0),
                CollegeAidRecord.cds_year.desc(),
            ),
        )
        .label("rank")
    )
    ranked = select(CollegeAidRecord.id.label("record_id"), rank).subquery()
    return (
        select(College, CollegeAidRecord)
        .join(CollegeAidRecord, CollegeAidRecord.college_id == College.id)
        .join(ranked, ranked.c.record_id == CollegeAidRecord.id)
        .where(ranked.c.rank == 1, College.hidden_at.is_(None))
        .order_by(College.name, College.state)
        .limit(MAX_ROWS)
    )


def fetch_latest_records(db: Session) -> list[tuple[College, CollegeAidRecord]]:
    return [(college, record) for college, record in db.execute(latest_records_query()).all()]
