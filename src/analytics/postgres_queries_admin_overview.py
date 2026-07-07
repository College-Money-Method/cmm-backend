"""Postgres aggregation queries for admin big-picture and geographic endpoints.

Split from postgres_queries_admin.py to keep file size manageable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.orm import Session

from src.schools.models import School
from src.workshops.models import WorkshopRegistration


# ── /admin/big-picture — Postgres parts ───────────────────────────────────────

def get_big_picture_counts(db: Session, date_from_dt: datetime, date_to_dt: datetime) -> dict:
    """Return total customer schools, engaged schools in range, and enrollment mix."""
    total_schools: int = db.execute(
        select(func.count()).select_from(School).where(School.is_current_customer.is_(True))
    ).scalar_one() or 0

    engaged: int = db.execute(
        select(func.count(distinct(WorkshopRegistration.school_id)))
        .where(
            and_(
                WorkshopRegistration.school_id.isnot(None),
                WorkshopRegistration.created_at >= date_from_dt,
                WorkshopRegistration.created_at <= date_to_dt,
            )
        )
    ).scalar_one() or 0

    # Enrollment mix for is_current_customer schools
    mix_stmt = (
        select(School.enrollment_range, func.count().label("cnt"))
        .where(School.is_current_customer.is_(True))
        .group_by(School.enrollment_range)
    )
    mix_rows = db.execute(mix_stmt).mappings().all()
    mix: dict[str, int] = {"small": 0, "medium": 0, "large": 0, "unknown": 0}
    _band_map = {"< 250": "small", "250-500": "medium", ">500": "large"}
    for row in mix_rows:
        band = _band_map.get(row["enrollment_range"] or "", "unknown")
        mix[band] += row["cnt"]

    return {
        "total_schools": total_schools,
        "engaged_schools_period": engaged,
        "enrollment_mix": mix,
    }


# ── /admin/geographic ─────────────────────────────────────────────────────────

def get_geographic_data(db: Session) -> dict:
    """by_state counts and enrollment band stats over trailing 365d."""
    state_stmt = (
        select(School.state, func.count().label("cnt"))
        .where(
            and_(
                School.is_current_customer.is_(True),
                School.state.isnot(None),
            )
        )
        .group_by(School.state)
        .order_by(func.count().desc())
    )
    by_state = [
        {"label": row["state"], "count": row["cnt"]}
        for row in db.execute(state_stmt).mappings().all()
    ]

    year_ago = datetime.now(timezone.utc) - timedelta(days=365)

    distinct_regs_sub = (
        select(
            WorkshopRegistration.school_id.label("sid"),
            func.count(distinct(WorkshopRegistration.email)).label("dist_regs"),
        )
        .where(WorkshopRegistration.created_at >= year_ago)
        .group_by(WorkshopRegistration.school_id)
        .subquery()
    )

    band_stmt = (
        select(
            School.enrollment_range,
            func.count(School.id).label("cnt"),
            func.avg(
                func.coalesce(distinct_regs_sub.c.dist_regs, 0).cast(float)
                / func.nullif(School.enrollment_9_12, 0)
                * 100
            ).label("avg_reach"),
        )
        .outerjoin(distinct_regs_sub, School.id == distinct_regs_sub.c.sid)
        .where(
            and_(
                School.is_current_customer.is_(True),
                School.enrollment_range.isnot(None),
                School.enrollment_9_12.isnot(None),
                School.enrollment_9_12 > 0,
            )
        )
        .group_by(School.enrollment_range)
        .order_by(School.enrollment_range)
    )

    _label_map = {"< 250": "Small (< 250)", "250-500": "Medium (250-500)", ">500": "Large (> 500)"}
    by_band = []
    for row in db.execute(band_stmt).mappings().all():
        avg_reach = row["avg_reach"]
        by_band.append({
            "label": _label_map.get(row["enrollment_range"], row["enrollment_range"]),
            "count": row["cnt"],
            "avg_reach_pct": round(float(avg_reach), 2) if avg_reach is not None else None,
        })

    return {"by_state": by_state, "by_enrollment_band": by_band}
