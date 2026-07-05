"""Postgres aggregation queries for counselor-facing analytics endpoints.

All functions accept a SQLAlchemy Session and return plain dicts/scalars.
No PostHog calls here — pure DB.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, and_, distinct, cast, Float
from sqlalchemy.orm import Session

from src.schools.models import School
from src.workshops.models import PortalMapping, Webinar, WorkshopRegistration


# ── workshops-detail ──────────────────────────────────────────────────────────

def get_webinars_for_school_in_range(
    db: Session,
    school_id: uuid.UUID,
    date_from: str,
    date_to: str | None,
) -> list[dict]:
    """Return per-webinar registration counts for a school within the date range.

    A webinar is included if:
      - It has a PortalMapping to the school, OR
      - It has at least one WorkshopRegistration from the school.
    Date filter is on webinar.start_datetime.
    """
    from src.workshops.models import Workshop

    # Build date bounds
    df = _parse_date_from(date_from)
    dt = _parse_date_to(date_to)

    # Webinar IDs via PortalMapping
    mapped_ids_q = select(PortalMapping.webinar_id).where(PortalMapping.school_id == school_id)

    # Webinar IDs via registrations
    reg_ids_q = (
        select(WorkshopRegistration.webinar_id)
        .where(WorkshopRegistration.school_id == school_id)
        .distinct()
    )

    # Core webinar query
    stmt = (
        select(
            Webinar.id,
            Webinar.zoom_webinar_id,
            Webinar.webinar_name,
            Webinar.start_datetime,
            Webinar.unmatched_participants_count,
            Workshop.name.label("workshop_name"),
        )
        .join(Workshop, Webinar.workshop_id == Workshop.id)
        .where(
            and_(
                Webinar.id.in_(mapped_ids_q.union(reg_ids_q)),
                Webinar.start_datetime >= df,
                Webinar.start_datetime <= dt,
            )
        )
        .order_by(Webinar.start_datetime)
    )

    webinar_rows = db.execute(stmt).mappings().all()

    if not webinar_rows:
        return []

    webinar_ids = [row["id"] for row in webinar_rows]

    # Registration counts per webinar for this school
    reg_stmt = (
        select(
            WorkshopRegistration.webinar_id,
            func.count().label("registered"),
            func.sum(
                func.cast(WorkshopRegistration.attended, Integer_type())
            ).label("attended_live"),
        )
        .where(
            and_(
                WorkshopRegistration.webinar_id.in_(webinar_ids),
                WorkshopRegistration.school_id == school_id,
            )
        )
        .group_by(WorkshopRegistration.webinar_id)
    )
    reg_counts = {
        row["webinar_id"]: row
        for row in db.execute(reg_stmt).mappings().all()
    }

    result = []
    for row in webinar_rows:
        wid = row["id"]
        reg = reg_counts.get(wid)
        registered = reg["registered"] if reg else 0
        attended_live = int(reg["attended_live"] or 0) if reg else 0
        result.append({
            "webinar_id": str(row["zoom_webinar_id"] or row["id"]),
            "workshop_name": row["workshop_name"] or row["webinar_name"] or "",
            "start_datetime": row["start_datetime"].isoformat() if row["start_datetime"] else None,
            "registered": registered,
            "attended_live": attended_live,
            "no_show": registered - attended_live,
            "joined_without_reg": row["unmatched_participants_count"],
            # recording_views and avg_percent_watched filled in by caller from PostHog
            "recording_views": 0,
            "avg_percent_watched": None,
            "_webinar_id_raw": wid,  # internal: for PostHog join
        })
    return result


def get_workshops_detail_totals(rows: list[dict]) -> dict:
    return {
        "registered": sum(r["registered"] for r in rows),
        "attended_live": sum(r["attended_live"] for r in rows),
        "no_show": sum(r["no_show"] for r in rows),
        "recording_views": sum(r["recording_views"] for r in rows),
    }


# ── reach ─────────────────────────────────────────────────────────────────────

def get_reach_data(db: Session, school_id: uuid.UUID) -> dict:
    """Compute distinct registrant count and reach % for one school."""
    school = db.get(School, school_id)
    if school is None:
        return {"distinct_registrants": 0, "enrollment": None, "reach_pct": None, "enrollment_range": None}

    distinct_q = (
        select(func.count(distinct(WorkshopRegistration.email)))
        .where(WorkshopRegistration.school_id == school_id)
    )
    distinct_registrants: int = db.execute(distinct_q).scalar_one() or 0

    enrollment = school.enrollment_9_12
    reach_pct: float | None = None
    if enrollment:
        reach_pct = round(distinct_registrants / enrollment * 100, 2)

    return {
        "distinct_registrants": distinct_registrants,
        "enrollment": enrollment,
        "reach_pct": reach_pct,
        "enrollment_range": school.enrollment_range,
    }


def get_reach_benchmark(db: Session, school_id: uuid.UUID, enrollment_range: str | None) -> dict | None:
    """Compute peer median reach % in the same enrollment band.

    Returns None if fewer than 3 peers with enrollment set.
    Never exposes peer names or IDs.
    """
    if not enrollment_range:
        return None

    # Build peer reach % for each peer school (same band, is_current_customer, has enrollment)
    # Subquery: distinct registrant count per school
    reg_sub = (
        select(
            WorkshopRegistration.school_id.label("sid"),
            func.count(distinct(WorkshopRegistration.email)).label("distinct_regs"),
        )
        .where(WorkshopRegistration.school_id.isnot(None))
        .group_by(WorkshopRegistration.school_id)
        .subquery()
    )

    # Peer schools in same band
    peer_stmt = (
        select(
            School.id,
            School.enrollment_9_12,
            func.coalesce(reg_sub.c.distinct_regs, 0).label("distinct_regs"),
        )
        .outerjoin(reg_sub, School.id == reg_sub.c.sid)
        .where(
            and_(
                School.is_current_customer.is_(True),
                School.enrollment_range == enrollment_range,
                School.enrollment_9_12.isnot(None),
                School.enrollment_9_12 > 0,
                School.id != school_id,
            )
        )
    )
    peers = db.execute(peer_stmt).mappings().all()

    if len(peers) < 3:
        return None

    reach_pcts = sorted(
        p["distinct_regs"] / p["enrollment_9_12"] * 100
        for p in peers
        if p["enrollment_9_12"]
    )

    n = len(reach_pcts)
    if n % 2 == 1:
        median = reach_pcts[n // 2]
    else:
        median = (reach_pcts[n // 2 - 1] + reach_pcts[n // 2]) / 2

    return {
        "median_reach_pct": round(median, 2),
        "peer_count": n,
        "above_median": False,  # set by caller after comparing to school's own reach_pct
    }


# ── library-coverage: Postgres counts ─────────────────────────────────────────

def get_library_published_counts(db: Session) -> dict:
    """Count published content_assets and topics."""
    from src.content.models import ContentAsset, Topic

    asset_count: int = db.execute(
        select(func.count()).select_from(ContentAsset).where(ContentAsset.status == "published")
    ).scalar_one() or 0

    topic_count: int = db.execute(
        select(func.count()).select_from(Topic).where(Topic.status == "published")
    ).scalar_one() or 0

    return {"published_assets": asset_count, "published_topics": topic_count}


# ── Date parsing helpers ───────────────────────────────────────────────────────

import re as _re

_RELATIVE_RE = _re.compile(r"^-(\d{1,4})d$")


def _parse_date_from(date_from: str) -> datetime:
    m = _RELATIVE_RE.match(date_from)
    if m:
        return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
    # Absolute YYYY-MM-DD
    return datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)


def _parse_date_to(date_to: str | None) -> datetime:
    if date_to is None:
        return datetime.now(timezone.utc)
    m = _RELATIVE_RE.match(date_to)
    if m:
        return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
    # Absolute YYYY-MM-DD — include full day
    return datetime.fromisoformat(date_to).replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc
    )


# ── SQLAlchemy Integer type alias (avoids import collision) ────────────────────

def Integer_type():  # noqa: N802
    from sqlalchemy import Integer
    return Integer
