"""Admin-only analytics endpoints — prefix /api/v1/analytics/admin.

All endpoints require AdminDep (super_admin only).
PostHog parts use 30-min cache; Postgres parts always fresh except
schools-health (30-min DB cache) and geographic (30-min DB cache).
"""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Query

from src.analytics import posthog as ph
from src.analytics.postgres_queries import _parse_date_from, _parse_date_to
from src.analytics.postgres_queries_admin import (
    get_active_schools_count,
    get_declining_schools,
    get_quiet_schools,
    get_registration_counts,
    get_stalled_activations,
    get_upcoming_webinars,
)
from src.analytics.postgres_queries_admin_overview import (
    get_big_picture_counts,
    get_geographic_data,
)
from src.analytics.schemas import (
    BigPictureData,
    EnrollmentBandStat,
    EnrollmentMix,
    GeographicData,
    PulseData,
    SchoolsHealthData,
    StalledSchool,
    QuietSchool,
    TopBreakdown,
    TranslationAnalytics,
    TrendMetric,
    UpcomingWebinar,
    WhatsWorkingData,
)
from src.analytics.translation_usage_queries import get_translation_analytics
from src.auth.deps import AdminDep
from src.config import settings
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/analytics/admin", tags=["analytics-admin"])

_ADMIN_TTL = timedelta(minutes=30)


def _check_configured() -> tuple[str, str]:
    from fastapi import HTTPException
    if not settings.posthog_api_key or not settings.posthog_project_id:
        raise HTTPException(status_code=503, detail="PostHog analytics not configured")
    return settings.posthog_api_key, settings.posthog_project_id


# ── /pulse ────────────────────────────────────────────────────────────────────

@router.get("/translation", response_model=TranslationAnalytics)
def get_translation_analytics_endpoint(
    current_user: AdminDep,
    db: DbDep,
    days: int = Query(30, ge=1, le=365, description="Trend window in days"),
) -> TranslationAnalytics:
    """Bedrock translation spend/usage: totals, by-locale, by-context, daily trend.

    Sourced from the translation_usage ledger (cache misses only) — cache hits
    cost nothing and aren't recorded, so these figures are true translation spend.
    """
    return TranslationAnalytics(**get_translation_analytics(db, days))


@router.get("/pulse", response_model=PulseData)
def get_pulse(
    current_user: AdminDep,
    db: DbDep,
) -> PulseData:
    api_key, project_id = _check_configured()

    # Postgres (always fresh)
    reg_counts = get_registration_counts(db)
    active_count = get_active_schools_count(db)
    upcoming = get_upcoming_webinars(db)

    # PostHog: 3 metrics batched into 2 HogQL queries
    # Query 1: content views this week (4 events counted together)
    content_key = ph._key(fn="pulse_content_views", admin=True)
    content_views: int
    if (cached := ph._db_get(db, content_key, None)) is not None:
        content_views = int(cached)
    else:
        content_hogql = (
            "SELECT count() FROM events "
            "WHERE event IN ('resource_card_click', 'topic_card_click', 'resource_viewed', 'topic_viewed') "
            "AND timestamp >= now() - INTERVAL 7 DAY"
        )
        try:
            rows = ph.get_hogql_query(api_key, project_id, content_hogql)
            content_views = int(rows[0][0]) if rows else 0
        except Exception:
            stale = ph._db_get_stale(db, content_key)
            content_views = int(stale) if stale is not None else 0
        ph._db_set(db, content_key, content_views)

    # Query 2: top search terms + top resource searches in one batched HogQL
    # (two separate queries — PostHog GROUP BY can't pivot two events cleanly)
    search_key = ph._key(fn="pulse_top_searches", admin=True)
    resource_search_key = ph._key(fn="pulse_top_resource_searches", admin=True)

    top_search_terms: list[TopBreakdown]
    top_resource_searches: list[TopBreakdown]

    cached_st = ph._db_get(db, search_key, None)
    cached_rs = ph._db_get(db, resource_search_key, None)

    if cached_st is not None and cached_rs is not None:
        top_search_terms = [TopBreakdown.model_validate(r) for r in cached_st]
        top_resource_searches = [TopBreakdown.model_validate(r) for r in cached_rs]
    else:
        try:
            st_hogql = (
                "SELECT properties.query, count() FROM events "
                "WHERE event = 'search_query' AND timestamp >= now() - INTERVAL 7 DAY "
                "AND isNotNull(properties.query) "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
            )
            rs_hogql = (
                "SELECT properties.query, count() FROM events "
                "WHERE event = 'resource_library_searched' AND timestamp >= now() - INTERVAL 7 DAY "
                "AND isNotNull(properties.query) "
                "GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
            )
            st_rows = ph.get_hogql_query(api_key, project_id, st_hogql)
            rs_rows = ph.get_hogql_query(api_key, project_id, rs_hogql)
            top_search_terms = [TopBreakdown(label=str(r[0]), count=float(r[1])) for r in st_rows if r[0]]
            top_resource_searches = [TopBreakdown(label=str(r[0]), count=float(r[1])) for r in rs_rows if r[0]]
        except Exception:
            top_search_terms = [TopBreakdown.model_validate(r) for r in (ph._db_get_stale(db, search_key) or [])]
            top_resource_searches = [TopBreakdown.model_validate(r) for r in (ph._db_get_stale(db, resource_search_key) or [])]

        ph._db_set(db, search_key, [t.model_dump() for t in top_search_terms])
        ph._db_set(db, resource_search_key, [t.model_dump() for t in top_resource_searches])

    return PulseData(
        registrations_today=reg_counts["registrations_today"],
        registrations_this_week=reg_counts["registrations_this_week"],
        active_schools_count=active_count,
        content_views_this_week=content_views,
        upcoming_webinars=[UpcomingWebinar(**w) for w in upcoming],
        top_search_terms=top_search_terms,
        top_resource_searches=top_resource_searches,
    )


# ── /schools-health ───────────────────────────────────────────────────────────

@router.get("/schools-health", response_model=SchoolsHealthData)
def get_schools_health(
    current_user: AdminDep,
    db: DbDep,
) -> SchoolsHealthData:
    cache_key = ph._key(fn="schools_health", admin=True)
    if (cached := ph._db_get(db, cache_key, None)) is not None:
        return SchoolsHealthData.model_validate(cached)

    stalled = [StalledSchool(**s) for s in get_stalled_activations(db)]
    quiet = [QuietSchool(**s) for s in get_quiet_schools(db)]
    declining = [QuietSchool(**s) for s in get_declining_schools(db)]

    result = SchoolsHealthData(
        stalled_activations=stalled,
        quiet_schools=quiet,
        declining_schools=declining,
    )
    ph._db_set(db, cache_key, result.model_dump())
    return result


# ── /big-picture ──────────────────────────────────────────────────────────────

@router.get("/big-picture", response_model=BigPictureData)
def get_big_picture(
    current_user: AdminDep,
    db: DbDep,
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
) -> BigPictureData:
    api_key, project_id = _check_configured()

    df_dt = _parse_date_from(date_from)
    dt_dt = _parse_date_to(date_to)

    pg = get_big_picture_counts(db, df_dt, dt_dt)

    # ONE batched HogQL: daily DAU + daily registrations (no school filter)
    cache_key = ph._key(fn="big_picture_posthog", df=date_from, dt=date_to)
    platform_dau: TrendMetric
    platform_regs: TrendMetric

    if (cached := ph._db_get(db, cache_key, None)) is not None:
        platform_dau = TrendMetric.model_validate(cached["dau"])
        platform_regs = TrendMetric.model_validate(cached["regs"])
    else:
        date_clause = ph._hogql_date_clause(date_from, date_to)
        hogql = (
            "SELECT toStartOfDay(timestamp) as day, "
            "  uniqIf(person_id, event = '$pageview') as dau, "
            "  countIf(event = 'workshop_registration_complete') as regs "
            "FROM events "
            f"WHERE {date_clause} "
            "GROUP BY day ORDER BY day"
        )
        try:
            rows = ph.get_hogql_query(api_key, project_id, hogql)
            days = [str(r[0])[:10] for r in rows]
            dau_data = [float(r[1]) for r in rows]
            reg_data = [float(r[2]) for r in rows]
            platform_dau = TrendMetric(total=int(sum(dau_data)), data=dau_data, days=days)
            platform_regs = TrendMetric(total=int(sum(reg_data)), data=reg_data, days=days)
        except Exception:
            stale = ph._db_get_stale(db, cache_key)
            if stale:
                platform_dau = TrendMetric.model_validate(stale["dau"])
                platform_regs = TrendMetric.model_validate(stale["regs"])
            else:
                platform_dau = TrendMetric(total=0, data=[], days=[])
                platform_regs = TrendMetric(total=0, data=[], days=[])

        ph._db_set(db, cache_key, {"dau": platform_dau.model_dump(), "regs": platform_regs.model_dump()})

    return BigPictureData(
        total_schools=pg["total_schools"],
        engaged_schools_period=pg["engaged_schools_period"],
        enrollment_mix=EnrollmentMix(**pg["enrollment_mix"]),
        platform_dau=platform_dau,
        platform_registrations=platform_regs,
    )


# ── /whats-working ────────────────────────────────────────────────────────────

@router.get("/whats-working", response_model=WhatsWorkingData)
def get_whats_working(
    current_user: AdminDep,
    db: DbDep,
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
) -> WhatsWorkingData:
    api_key, project_id = _check_configured()

    cache_key = ph._key(fn="whats_working", df=date_from, dt=date_to)
    if (cached := ph._db_get(db, cache_key, None)) is not None:
        return WhatsWorkingData.model_validate(cached)

    date_clause = ph._hogql_date_clause(date_from, date_to)

    def _run(hogql: str) -> list[TopBreakdown]:
        rows = ph.get_hogql_query(api_key, project_id, hogql)
        return [TopBreakdown(label=str(r[0]), count=float(r[1])) for r in rows if r[0]]

    try:
        resources = _run(
            f"SELECT properties.resource_name, count() FROM events "
            f"WHERE event = 'resource_card_click' AND {date_clause} "
            f"AND isNotNull(properties.resource_name) GROUP BY 1 ORDER BY 2 DESC LIMIT 15"
        )
        topics = _run(
            f"SELECT properties.topic_title, count() FROM events "
            f"WHERE event = 'topic_card_click' AND {date_clause} "
            f"AND isNotNull(properties.topic_title) GROUP BY 1 ORDER BY 2 DESC LIMIT 15"
        )
        workshops = _run(
            f"SELECT properties.workshop_name, count() FROM events "
            f"WHERE event = 'workshop_registration_complete' AND {date_clause} "
            f"AND isNotNull(properties.workshop_name) GROUP BY 1 ORDER BY 2 DESC LIMIT 10"
        )
        zero_results = _run(
            f"SELECT properties.query, count() FROM events "
            f"WHERE event = 'search_query' AND {date_clause} "
            # toInt64OrNull is unsupported in HogQL — toInt handles both string and numeric
            f"AND toInt(ifNull(properties.result_count, '1')) = 0 "
            f"AND isNotNull(properties.query) GROUP BY 1 ORDER BY 2 DESC LIMIT 15"
        )
    except Exception:
        stale = ph._db_get_stale(db, cache_key)
        if stale:
            return WhatsWorkingData.model_validate(stale)
        return WhatsWorkingData(top_resources=[], top_topics=[], top_workshops=[], zero_result_searches=[])

    result = WhatsWorkingData(
        top_resources=resources,
        top_topics=topics,
        top_workshops=workshops,
        zero_result_searches=zero_results,
    )
    ph._db_set(db, cache_key, result.model_dump())
    return result


# ── /geographic ───────────────────────────────────────────────────────────────

@router.get("/geographic", response_model=GeographicData)
def get_geographic(
    current_user: AdminDep,
    db: DbDep,
) -> GeographicData:
    cache_key = ph._key(fn="geographic", admin=True)
    if (cached := ph._db_get(db, cache_key, None)) is not None:
        return GeographicData.model_validate(cached)

    geo = get_geographic_data(db)
    result = GeographicData(
        by_state=[TopBreakdown(label=s["label"], count=s["count"]) for s in geo["by_state"]],
        by_enrollment_band=[EnrollmentBandStat(**b) for b in geo["by_enrollment_band"]],
    )
    ph._db_set(db, cache_key, result.model_dump())
    return result
