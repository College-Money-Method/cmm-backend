"""Postgres aggregation queries for admin pulse and schools-health endpoints.

big-picture and geographic queries live in postgres_queries_admin_overview.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, distinct, func, select
from sqlalchemy.orm import Session

from src.schools.models import School
from src.workshops.models import Webinar, WorkshopRegistration, Workshop


# ── /admin/pulse — Postgres parts ─────────────────────────────────────────────

def get_registration_counts(db: Session) -> dict:
    """Count registrations created today and in the last 7 days (school-linked only)."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    base = select(func.count()).select_from(WorkshopRegistration).where(
        WorkshopRegistration.school_id.isnot(None)
    )
    today_count: int = db.execute(
        base.where(WorkshopRegistration.created_at >= today_start)
    ).scalar_one() or 0

    week_count: int = db.execute(
        base.where(WorkshopRegistration.created_at >= week_ago)
    ).scalar_one() or 0

    return {"registrations_today": today_count, "registrations_this_week": week_count}


def get_active_schools_count(db: Session) -> int:
    """Schools with at least one school-linked registration in the last 7 days."""
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    result = db.execute(
        select(func.count(distinct(WorkshopRegistration.school_id)))
        .where(
            and_(
                WorkshopRegistration.school_id.isnot(None),
                WorkshopRegistration.created_at >= week_ago,
            )
        )
    ).scalar_one()
    return result or 0


def get_upcoming_webinars(db: Session) -> list[dict]:
    """Webinars starting in the next 7 days with registration counts."""
    now = datetime.now(timezone.utc)
    week_out = now + timedelta(days=7)

    reg_sub = (
        select(
            WorkshopRegistration.webinar_id.label("wid"),
            func.count().label("reg_count"),
        )
        .group_by(WorkshopRegistration.webinar_id)
        .subquery()
    )

    stmt = (
        select(
            Webinar.id,
            Webinar.zoom_webinar_id,
            Webinar.webinar_name,
            Webinar.start_datetime,
            Workshop.name.label("workshop_name"),
            func.coalesce(reg_sub.c.reg_count, 0).label("registered"),
        )
        .join(Workshop, Webinar.workshop_id == Workshop.id)
        .outerjoin(reg_sub, Webinar.id == reg_sub.c.wid)
        .where(
            and_(
                Webinar.start_datetime >= now,
                Webinar.start_datetime <= week_out,
            )
        )
        .order_by(Webinar.start_datetime)
    )

    rows = db.execute(stmt).mappings().all()
    return [
        {
            "webinar_id": str(row["zoom_webinar_id"] or row["id"]),
            "workshop_name": row["workshop_name"] or row["webinar_name"] or "",
            "start_datetime": row["start_datetime"].isoformat() if row["start_datetime"] else None,
            "registered": row["registered"],
        }
        for row in rows
    ]


# ── /admin/schools-health ──────────────────────────────────────────────────────

def get_stalled_activations(db: Session) -> list[dict]:
    """is_current_customer AND created_at >= now-90d AND zero registrations ever."""
    ninety_days_ago = datetime.now(timezone.utc) - timedelta(days=90)

    # Schools with any registration
    has_reg_sub = (
        select(WorkshopRegistration.school_id)
        .where(WorkshopRegistration.school_id.isnot(None))
        .distinct()
        .subquery()
    )

    stmt = (
        select(School)
        .where(
            and_(
                School.is_current_customer.is_(True),
                School.created_at >= ninety_days_ago,
                School.id.not_in(select(has_reg_sub.c.school_id)),
            )
        )
        .order_by(School.created_at.desc())
    )

    return [
        {
            "id": str(s.id),
            "name": s.name,
            "state": s.state,
            "enrollment_range": s.enrollment_range,
            "created_at": s.created_at.isoformat(),
        }
        for s in db.execute(stmt).scalars().all()
    ]


def get_quiet_schools(db: Session) -> list[dict]:
    """0 regs last 30d but >0 in prior 60d (days 30-90 ago)."""
    now = datetime.now(timezone.utc)
    d30 = now - timedelta(days=30)
    d90 = now - timedelta(days=90)

    # Recent (last 30d) reg counts per school
    recent_sub = (
        select(
            WorkshopRegistration.school_id.label("sid"),
            func.count().label("cnt"),
        )
        .where(
            and_(
                WorkshopRegistration.school_id.isnot(None),
                WorkshopRegistration.created_at >= d30,
            )
        )
        .group_by(WorkshopRegistration.school_id)
        .subquery()
    )

    # Prior (30-90d) reg counts per school
    prior_sub = (
        select(
            WorkshopRegistration.school_id.label("sid"),
            func.count().label("cnt"),
        )
        .where(
            and_(
                WorkshopRegistration.school_id.isnot(None),
                WorkshopRegistration.created_at >= d90,
                WorkshopRegistration.created_at < d30,
            )
        )
        .group_by(WorkshopRegistration.school_id)
        .subquery()
    )

    stmt = (
        select(
            School,
            func.coalesce(recent_sub.c.cnt, 0).label("recent_regs"),
            func.coalesce(prior_sub.c.cnt, 0).label("prior_regs"),
        )
        .outerjoin(recent_sub, School.id == recent_sub.c.sid)
        .outerjoin(prior_sub, School.id == prior_sub.c.sid)
        .where(
            and_(
                School.is_current_customer.is_(True),
                func.coalesce(recent_sub.c.cnt, 0) == 0,
                func.coalesce(prior_sub.c.cnt, 0) > 0,
            )
        )
        .order_by(School.name)
    )

    return [
        {
            "id": str(row.School.id),
            "name": row.School.name,
            "state": row.School.state,
            "enrollment_range": row.School.enrollment_range,
            "recent_regs": row.recent_regs,
            "prior_regs": row.prior_regs,
        }
        for row in db.execute(stmt).all()
    ]


def get_declining_schools(db: Session) -> list[dict]:
    """recent 30d < 50% of prior 30d (days 30-60 ago) AND prior > 0."""
    now = datetime.now(timezone.utc)
    d30 = now - timedelta(days=30)
    d60 = now - timedelta(days=60)

    recent_sub = (
        select(
            WorkshopRegistration.school_id.label("sid"),
            func.count().label("cnt"),
        )
        .where(
            and_(
                WorkshopRegistration.school_id.isnot(None),
                WorkshopRegistration.created_at >= d30,
            )
        )
        .group_by(WorkshopRegistration.school_id)
        .subquery()
    )

    prior_sub = (
        select(
            WorkshopRegistration.school_id.label("sid"),
            func.count().label("cnt"),
        )
        .where(
            and_(
                WorkshopRegistration.school_id.isnot(None),
                WorkshopRegistration.created_at >= d60,
                WorkshopRegistration.created_at < d30,
            )
        )
        .group_by(WorkshopRegistration.school_id)
        .subquery()
    )

    stmt = (
        select(
            School,
            func.coalesce(recent_sub.c.cnt, 0).label("recent_regs"),
            func.coalesce(prior_sub.c.cnt, 0).label("prior_regs"),
        )
        .outerjoin(recent_sub, School.id == recent_sub.c.sid)
        .outerjoin(prior_sub, School.id == prior_sub.c.sid)
        .where(
            and_(
                School.is_current_customer.is_(True),
                func.coalesce(prior_sub.c.cnt, 0) > 0,
                func.coalesce(recent_sub.c.cnt, 0) < func.coalesce(prior_sub.c.cnt, 0) * 0.5,
            )
        )
        .order_by(School.name)
    )

    return [
        {
            "id": str(row.School.id),
            "name": row.School.name,
            "state": row.School.state,
            "enrollment_range": row.School.enrollment_range,
            "recent_regs": row.recent_regs,
            "prior_regs": row.prior_regs,
        }
        for row in db.execute(stmt).all()
    ]


