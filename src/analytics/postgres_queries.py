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
    cycle_id: uuid.UUID | None = None,
    range_numbers: bool = False,
) -> list[dict]:
    """Return per-webinar registration counts for a school.

    A webinar is included if:
      - It has a PortalMapping to the school, OR
      - It has at least one WorkshopRegistration from the school.
    Scope: when cycle_id is given, filter by webinar.cycle_id (a cycle's webinars
    stay browsable outside its calendar dates); otherwise by start_datetime range.

    range_numbers: when True (used with cycle_id — "all workshops, numbers for
    the time period selected"), the COUNTS are date-scoped while the rows are
    not: registrations count only those made within [date_from, date_to]
    (by registration_time, falling back to created_at), and attendees zero out
    for webinars whose start_datetime falls outside the range (attendance can
    only happen at the webinar itself).
    """
    from src.workshops.models import Workshop

    # Webinar IDs via PortalMapping
    mapped_ids_q = select(PortalMapping.webinar_id).where(PortalMapping.school_id == school_id)

    # Webinar IDs via registrations
    reg_ids_q = (
        select(WorkshopRegistration.webinar_id)
        .where(WorkshopRegistration.school_id == school_id)
        .distinct()
    )

    if cycle_id is not None:
        scope_clause = Webinar.cycle_id == cycle_id
    else:
        df = _parse_date_from(date_from)
        dt = _parse_date_to(date_to)
        scope_clause = and_(Webinar.start_datetime >= df, Webinar.start_datetime <= dt)

    # Core webinar query
    stmt = (
        select(
            Webinar.id,
            Webinar.zoom_webinar_id,
            Webinar.webinar_name,
            Webinar.start_datetime,
            Webinar.unmatched_participants_count,
            Workshop.name.label("workshop_name"),
            Workshop.sequence_number.label("sequence_number"),
        )
        .join(Workshop, Webinar.workshop_id == Workshop.id)
        .where(
            and_(
                Webinar.id.in_(mapped_ids_q.union(reg_ids_q)),
                scope_clause,
            )
        )
        .order_by(Webinar.start_datetime)
    )

    webinar_rows = db.execute(stmt).mappings().all()

    if not webinar_rows:
        return []

    webinar_ids = [row["id"] for row in webinar_rows]

    # Registration counts per webinar for this school. In range_numbers mode the
    # registration date (registration_time, falling back to created_at) must sit
    # inside the selected range.
    reg_where = [
        WorkshopRegistration.webinar_id.in_(webinar_ids),
        WorkshopRegistration.school_id == school_id,
    ]
    if range_numbers:
        df = _parse_date_from(date_from)
        dt = _parse_date_to(date_to)
        reg_where.append(
            func.coalesce(
                WorkshopRegistration.registration_time, WorkshopRegistration.created_at
            ).between(df, dt)
        )
    reg_stmt = (
        select(
            WorkshopRegistration.webinar_id,
            func.count().label("registered"),
            func.sum(
                func.cast(WorkshopRegistration.attended, Integer_type())
            ).label("attended_live"),
        )
        .where(and_(*reg_where))
        .group_by(WorkshopRegistration.webinar_id)
    )
    reg_counts = {
        row["webinar_id"]: row
        for row in db.execute(reg_stmt).mappings().all()
    }

    # range_numbers: attendance happens AT the webinar, so attendees zero out
    # when the webinar's start_datetime falls outside the selected range.
    range_df = _parse_date_from(date_from) if range_numbers else None
    range_dt = _parse_date_to(date_to) if range_numbers else None

    result = []
    for row in webinar_rows:
        wid = row["id"]
        reg = reg_counts.get(wid)
        registered = reg["registered"] if reg else 0
        attended_live = int(reg["attended_live"] or 0) if reg else 0
        if range_numbers and range_df is not None and range_dt is not None:
            start = row["start_datetime"]
            if start is None or not (range_df <= start <= range_dt):
                attended_live = 0
        result.append({
            # Always use internal UUID — matches PostHog properties.webinar_id
            "webinar_id": str(wid),
            "workshop_name": row["workshop_name"] or row["webinar_name"] or "",
            "start_datetime": row["start_datetime"].isoformat() if row["start_datetime"] else None,
            "registered": registered,
            "attended_live": attended_live,
            "no_show": registered - attended_live,
            "joined_without_reg": row["unmatched_participants_count"],
            "sequence_number": row["sequence_number"],
            # recording_views, avg_percent_watched, detail_views, resource_views filled by caller from PostHog
            "recording_views": 0,
            "avg_percent_watched": None,
            "detail_views": 0,
            "resource_views": 0,
            "_webinar_id_raw": wid,  # internal UUID (same as webinar_id now, kept for compatibility)
        })
    return result


def get_webinar_by_id(db: Session, webinar_id: uuid.UUID) -> dict | None:
    """Lookup a webinar by its internal UUID.

    PostHog properties.webinar_id is the internal webinar UUID string, not
    the Zoom numeric ID. Returns start_datetime + workshop_name, or None.
    """
    from src.workshops.models import Workshop

    stmt = (
        select(
            Webinar.id,
            Webinar.webinar_name,
            Webinar.start_datetime,
            Workshop.name.label("workshop_name"),
        )
        .join(Workshop, Webinar.workshop_id == Workshop.id)
        .where(Webinar.id == webinar_id)
        .limit(1)
    )
    row = db.execute(stmt).mappings().first()
    if row is None:
        return None
    return {
        "webinar_id": str(row["id"]),
        "workshop_name": row["workshop_name"] or row["webinar_name"] or "",
        "start_datetime": row["start_datetime"],  # datetime | None, tz-aware
    }



def get_workshops_detail_totals(rows: list[dict]) -> dict:
    return {
        "registered": sum(r["registered"] for r in rows),
        "attended_live": sum(r["attended_live"] for r in rows),
        "no_show": sum(r["no_show"] for r in rows),
        "recording_views": sum(r["recording_views"] for r in rows),
        "detail_views": sum(r.get("detail_views", 0) for r in rows),
        "resource_views": sum(r.get("resource_views", 0) for r in rows),
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
