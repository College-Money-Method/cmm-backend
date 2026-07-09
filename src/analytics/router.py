"""Analytics endpoints — proxy PostHog queries with school-level access control.

Existing 4 endpoints (overview/workshop/content/search) preserve exact response
shapes. New hub endpoints added below.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from fastapi import APIRouter, HTTPException, Query
from src.analytics import posthog as ph
from src.analytics import posthog_batched as phb
from src.analytics.schemas import (
    ContentData,
    LibraryCoverageData,
    OverviewData,
    PeakUsageCell,
    PeakUsageData,
    ReachBenchmark,
    ReachData,
    ResourceUsedRow,
    SearchData,
    TrendMetric,
    WebinarDetail,
    WorkshopData,
    WorkshopsDetailData,
    WorkshopsDetailTotals,
    WorkshopTimelineTrends,
    WorkshopVideoStats,
)
from src.analytics.postgres_queries import (
    get_library_published_counts,
    get_reach_benchmark,
    get_reach_data,
    get_webinar_by_id,
    get_webinars_for_school_in_range,
    get_workshops_detail_totals,
)
from src.auth.deps import CounselorDep
from src.config import settings
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])


def _resolve_school(current_user: CounselorDep, school_id_param: str | None) -> str | None:
    """Admins can filter by any school or see all; counselors are locked to their school."""
    if current_user.role == "super_admin":
        return school_id_param or None
    return str(current_user.school_id) if current_user.school_id else None


def _check_configured() -> tuple[str, str]:
    if not settings.posthog_api_key or not settings.posthog_project_id:
        raise HTTPException(status_code=503, detail="PostHog analytics not configured")
    return settings.posthog_api_key, settings.posthog_project_id


# ── Existing endpoints (response shapes MUST NOT change) ──────────────────────

@router.get("/overview", response_model=OverviewData)
def get_overview(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
) -> OverviewData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to, cycle_name=cycle_name, db=db)
    # ONE PostHog round trip for both series (was 2 sequential calls).
    # Note: school scoping is now the event-property super-prop for DAU too
    # (was person-property) — first-visit pageviews before registration are excluded.
    trends = phb.get_batched_trends(api_key, project_id, [
        {"key": "dau", "event": "$pageview", "math": "dau"},
        {"key": "sign_ins", "event": "user_signed_in"},
    ], **opts)
    return OverviewData(dau=trends["dau"], sign_ins=trends["sign_ins"])


@router.get("/workshop", response_model=WorkshopData)
def get_workshop(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
) -> WorkshopData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to, cycle_name=cycle_name, db=db)
    # TWO PostHog round trips total (was 6 sequential calls)
    trends = phb.get_batched_trends(api_key, project_id, [
        {"key": "watch_recordings", "event": "workshop_watch_recording"},
        {"key": "registrations_opened", "event": "workshop_register_open"},
        {"key": "registrations", "event": "workshop_registration_complete"},
    ], **opts)
    breakdowns = phb.get_batched_breakdowns(api_key, project_id, [
        {"key": "top_videos", "event": "video_session_end", "prop": "workshop_name", "limit": 10},
        {"key": "top_watchtime", "event": "video_session_end", "prop": "workshop_name",
         "math": "avg", "math_prop": "total_watch_seconds", "limit": 10},
        {"key": "milestone_dropoff", "event": "recording_progress", "prop": "milestone_pct", "order": "label_num"},
    ], **opts)
    return WorkshopData(
        watch_recordings=trends["watch_recordings"],
        registrations_opened=trends["registrations_opened"],
        registrations=trends["registrations"],
        top_videos=breakdowns["top_videos"],
        top_watchtime=breakdowns["top_watchtime"],
        milestone_dropoff=breakdowns["milestone_dropoff"],
    )


@router.get("/content", response_model=ContentData)
def get_content(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
) -> ContentData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to, cycle_name=cycle_name, db=db)
    # TWO PostHog round trips total (was 7 sequential calls)
    trends = phb.get_batched_trends(api_key, project_id, [
        {"key": "resource_clicks", "event": "resource_card_click"},
        {"key": "topic_clicks", "event": "topic_card_click"},
        {"key": "resource_views", "event": "resource_viewed"},
        {"key": "resource_link_opens", "event": "resource_detail_external_link_click"},
    ], **opts)
    breakdowns = phb.get_batched_breakdowns(api_key, project_id, [
        {"key": "top_resources", "event": "resource_card_click", "prop": "resource_name", "limit": 10},
        {"key": "top_topics", "event": "topic_card_click", "prop": "topic_title", "limit": 10},
        {"key": "top_pages", "event": "$pageview", "prop": "$pathname", "limit": 10},
    ], **opts)
    return ContentData(
        resource_clicks=trends["resource_clicks"],
        topic_clicks=trends["topic_clicks"],
        top_resources=breakdowns["top_resources"],
        top_topics=breakdowns["top_topics"],
        resource_views=trends["resource_views"],
        resource_link_opens=trends["resource_link_opens"],
        top_pages=breakdowns["top_pages"],
    )


@router.get("/search", response_model=SearchData)
def get_search(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
) -> SearchData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to, cycle_name=cycle_name, db=db)
    # TWO PostHog round trips total (was 4 sequential calls)
    trends = phb.get_batched_trends(api_key, project_id, [
        {"key": "searches", "event": "search_query"},
        {"key": "library_searches", "event": "resource_library_searched"},
    ], **opts)
    breakdowns = phb.get_batched_breakdowns(api_key, project_id, [
        {"key": "top_queries", "event": "search_query", "prop": "query", "limit": 8},
        {"key": "top_library_queries", "event": "resource_library_searched", "prop": "query", "limit": 8},
    ], **opts)
    return SearchData(
        searches=trends["searches"],
        top_queries=breakdowns["top_queries"],
        library_searches=trends["library_searches"],
        top_library_queries=breakdowns["top_library_queries"],
    )


# ── New hub endpoints ─────────────────────────────────────────────────────────

@router.get("/workshops-detail", response_model=WorkshopsDetailData)
def get_workshops_detail(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
    cycle_id: uuid.UUID | None = Query(default=None),
) -> WorkshopsDetailData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    if not sid:
        raise HTTPException(status_code=400, detail="school_id is required for workshops-detail")

    school_uuid = uuid.UUID(sid)
    # cycle_id filters webinars by their cycle (families browse cycle content
    # outside the cycle's calendar dates); date range is the fallback
    rows = get_webinars_for_school_in_range(db, school_uuid, date_from, date_to, cycle_id=cycle_id)

    if rows:
        date_clause = ph._hogql_date_clause(date_from, date_to)
        school_clause = ph._hogql_school_clause(sid)
        cycle_clause = ph._hogql_cycle_clause(cycle_name)

        # Query 1: recording views + avg % watched per webinar_id (video_session_end)
        hogql_rec = (
            "SELECT properties.webinar_id, count(), avg(toFloat(ifNull(properties.percent_watched, '0'))) "
            "FROM events "
            f"WHERE event = 'video_session_end' AND {date_clause}{school_clause}{cycle_clause} "
            "AND isNotNull(properties.webinar_id) "
            "GROUP BY properties.webinar_id"
        )
        # Query 2: detail views per webinar_id (workshop_detail_view)
        hogql_detail = (
            "SELECT properties.webinar_id, count() "
            "FROM events "
            f"WHERE event = 'workshop_detail_view' AND {date_clause}{school_clause}{cycle_clause} "
            "AND isNotNull(properties.webinar_id) "
            "GROUP BY properties.webinar_id"
        )
        # Query 3: resource views per webinar_id (resource_viewed WHERE via='workshop')
        # properties.from holds the webinar_id for resources opened from a workshop detail page
        hogql_res = (
            "SELECT properties.from, count() "
            "FROM events "
            f"WHERE event = 'resource_viewed' AND {date_clause}{school_clause}{cycle_clause} "
            "AND properties.via = 'workshop' "
            "AND isNotNull(properties.from) "
            "GROUP BY properties.from"
        )
        # ph_map: webinar_id → (recording_views, avg_percent_watched)
        ph_map: dict[str, tuple[int, float | None]] = {}
        # detail_map: webinar_id → detail_views count
        detail_map: dict[str, int] = {}
        # res_map: webinar_id → resource_views count
        res_map: dict[str, int] = {}
        try:
            rec_rows = ph.get_hogql_query(api_key, project_id, hogql_rec)
            ph_map = {
                str(r[0]): (int(r[1]), float(r[2]) if r[2] is not None else None)
                for r in rec_rows if r[0]
            }
        except Exception:
            pass
        try:
            detail_rows = ph.get_hogql_query(api_key, project_id, hogql_detail)
            detail_map = {str(r[0]): int(r[1]) for r in detail_rows if r[0]}
        except Exception:
            pass
        try:
            res_rows = ph.get_hogql_query(api_key, project_id, hogql_res)
            res_map = {str(r[0]): int(r[1]) for r in res_rows if r[0]}
        except Exception:
            pass

        for row in rows:
            vid = row["webinar_id"]
            if vid in ph_map:
                row["recording_views"] = ph_map[vid][0]
                row["avg_percent_watched"] = ph_map[vid][1]
            row["detail_views"] = detail_map.get(vid, 0)
            row["resource_views"] = res_map.get(vid, 0)

    webinars = [
        WebinarDetail(
            webinar_id=r["webinar_id"],
            workshop_name=r["workshop_name"],
            start_datetime=r["start_datetime"],
            registered=r["registered"],
            attended_live=r["attended_live"],
            no_show=r["no_show"],
            joined_without_reg=r["joined_without_reg"],
            recording_views=r["recording_views"],
            avg_percent_watched=r["avg_percent_watched"],
            sequence_number=r.get("sequence_number"),
            detail_views=r.get("detail_views", 0),
            resource_views=r.get("resource_views", 0),
        )
        for r in rows
    ]
    totals_dict = get_workshops_detail_totals(rows)
    return WorkshopsDetailData(
        webinars=webinars,
        totals=WorkshopsDetailTotals(**totals_dict),
    )


@router.get("/reach", response_model=ReachData)
def get_reach(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
) -> ReachData:
    sid = _resolve_school(current_user, school_id)
    if not sid:
        raise HTTPException(status_code=400, detail="school_id is required (super_admin must pass school_id)")

    school_uuid = uuid.UUID(sid)
    reach = get_reach_data(db, school_uuid)
    benchmark_raw = get_reach_benchmark(db, school_uuid, reach["enrollment_range"])

    benchmark: ReachBenchmark | None = None
    if benchmark_raw is not None:
        above = (reach["reach_pct"] or 0) > benchmark_raw["median_reach_pct"]
        benchmark = ReachBenchmark(
            median_reach_pct=benchmark_raw["median_reach_pct"],
            peer_count=benchmark_raw["peer_count"],
            above_median=above,
        )

    return ReachData(
        distinct_registrants=reach["distinct_registrants"],
        enrollment=reach["enrollment"],
        reach_pct=reach["reach_pct"],
        enrollment_range=reach["enrollment_range"],
        benchmark=benchmark,
    )


@router.get("/peak-usage", response_model=PeakUsageData)
def get_peak_usage(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
) -> PeakUsageData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)

    cache_key = ph._key(fn="peak_usage", school_id=sid, df=date_from, dt=date_to, cyc=cycle_name)
    if (cached := ph._db_get(db, cache_key, sid)) is not None:
        return PeakUsageData.model_validate(cached)

    date_clause = ph._hogql_date_clause(date_from, date_to)
    school_clause = ph._hogql_school_clause(sid)
    cycle_clause = ph._hogql_cycle_clause(cycle_name)
    hogql = (
        "SELECT toDayOfWeek(timestamp), toHour(timestamp), count() "
        "FROM events "
        f"WHERE event = '$pageview' AND {date_clause}{school_clause}{cycle_clause} "
        "GROUP BY 1, 2 ORDER BY 1, 2"
    )
    try:
        rows = ph.get_hogql_query(api_key, project_id, hogql)
    except Exception:
        stale = ph._db_get_stale(db, cache_key)
        if stale:
            return PeakUsageData.model_validate(stale)
        return PeakUsageData(cells=[], max_count=0)

    cells = [PeakUsageCell(day=int(r[0]), hour=int(r[1]), count=int(r[2])) for r in rows]
    max_count = max((c.count for c in cells), default=0)
    result = PeakUsageData(cells=cells, max_count=max_count)
    ph._db_set(db, cache_key, result.model_dump())
    return result


@router.get("/workshop-timeline", response_model=WorkshopTimelineTrends)
def get_workshop_timeline(
    current_user: CounselorDep,
    db: DbDep,
    webinar_id: str = Query(...),
    school_id: str | None = Query(default=None),
    weeks_before: int = Query(default=4, ge=0, le=52),
    weeks_after: int = Query(default=4, ge=0, le=52),
) -> WorkshopTimelineTrends:
    """Per-webinar windowed engagement around start_datetime.

    webinar_id = internal webinar UUID (matches PostHog properties.webinar_id).
    Window = [start − weeks_before*7d, start + weeks_after*7d].
    Returns 4 daily TrendMetric series, video aggregate stats, and resource breakdown.
    """
    api_key, project_id = _check_configured()
    # Validate webinar_id as UUID — PostHog properties.webinar_id is the internal UUID
    try:
        webinar_uuid = uuid.UUID(webinar_id)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"webinar_id must be a valid UUID: {webinar_id!r}")
    phb._validate_webinar_id(webinar_id)  # belt-and-suspenders: rejects non-UUID chars
    sid = _resolve_school(current_user, school_id)

    cache_key = ph._key(
        fn="workshop_timeline",
        webinar_id=webinar_id,
        school_id=sid,
        weeks_before=weeks_before,
        weeks_after=weeks_after,
    )
    if (cached := ph._db_get(db, cache_key, sid)) is not None:
        return WorkshopTimelineTrends.model_validate(cached)

    # Lookup webinar metadata from Postgres by internal UUID
    webinar = get_webinar_by_id(db, webinar_uuid)
    if webinar is None:
        raise HTTPException(status_code=404, detail=f"Webinar {webinar_id!r} not found")
    if webinar["start_datetime"] is None:
        raise HTTPException(
            status_code=422,
            detail=f"Webinar {webinar_id!r} has no start_datetime; cannot compute timeline window",
        )

    # Compute window from adjustable weeks_before / weeks_after params
    start_dt = webinar["start_datetime"]
    days_before = weeks_before * 7
    days_after = weeks_after * 7
    total_days = days_before + days_after + 1  # inclusive of start day
    window_start: date = (start_dt - timedelta(days=days_before)).date()
    window_end: date = (start_dt + timedelta(days=days_after)).date()
    days = [(window_start + timedelta(days=i)).isoformat() for i in range(total_days)]

    webinar_windows: list[phb.WebinarWindow] = [
        {"webinar_id": webinar_id, "window_start": window_start, "window_end": window_end}
    ]

    # resource_viewed carries the origin workshop in properties.from (its
    # webinar_id is empty), so match on `from` and add only the via='workshop'
    # filter — the windowed helper matches from=<webinar_id> per window.
    event_specs: list[phb.EventSpec] = [
        {"key": "registrations", "event": "workshop_registration_complete"},
        {"key": "detail_views", "event": "workshop_detail_view"},
        {"key": "video_watch_count", "event": "video_session_end"},
        {
            "key": "resource_views",
            "event": "resource_viewed",
            "match_prop": "from",
            "extra_filter": " AND properties.via = 'workshop'",
        },
    ]

    try:
        raw = phb.get_windowed_trends_by_webinar(api_key, project_id, webinar_windows, event_specs, sid, db)
    except Exception as exc:
        stale = ph._db_get_stale(db, cache_key)
        if stale:
            return WorkshopTimelineTrends.model_validate(stale)
        raise HTTPException(status_code=503, detail="PostHog unavailable") from exc

    # Build each TrendMetric with zero-fill
    def _trend(event_key: str) -> TrendMetric:
        pairs = raw.get(event_key, {}).get(webinar_id, [])
        day_count: dict[str, int] = {}
        for d, cnt in pairs:
            day_count[d] = day_count.get(d, 0) + cnt
        data = [float(day_count.get(d, 0)) for d in days]
        return TrendMetric(total=int(sum(data)), data=data, days=days)

    ws_date = window_start.isoformat()
    we_date = window_end.isoformat()

    # -- Video aggregate stats (video_session_end filtered by webinar_id) ---------
    video_stats = WorkshopVideoStats(total_plays=0, total_minutes_watched=0, avg_percent_watched=None)
    try:
        video_hogql = (
            "SELECT "
            "  count() AS total_plays, "
            "  round(sum(toFloat(ifNull(properties.total_watch_seconds, '0'))) / 60) AS total_minutes_watched, "
            "  avg(toFloat(ifNull(properties.percent_watched, '0'))) AS avg_percent_watched "
            "FROM events "
            f"WHERE event = 'video_session_end' "
            f"  AND properties.webinar_id = '{webinar_id}' "
            f"  AND timestamp >= toDate('{ws_date}') "
            f"  AND timestamp <= toDate('{we_date}') + INTERVAL 1 DAY"
        )
        video_rows = ph.get_hogql_query(api_key, project_id, video_hogql)
        if video_rows:
            r = video_rows[0]
            total_plays = int(r[0]) if r[0] is not None else 0
            total_minutes = int(r[1]) if r[1] is not None else 0
            avg_pct = float(r[2]) if r[2] is not None and total_plays > 0 else None
            video_stats = WorkshopVideoStats(
                total_plays=total_plays,
                total_minutes_watched=total_minutes,
                avg_percent_watched=avg_pct,
            )
    except Exception:
        pass  # degrade gracefully — zeros/None already set above

    # -- Resources-used breakdown (resource_viewed via=workshop, from=webinar_id) --
    resources_used: list[ResourceUsedRow] = []
    try:
        resources_hogql = (
            "SELECT "
            "  properties.asset_name AS resource_name, "
            "  count() AS cnt "
            "FROM events "
            f"WHERE event = 'resource_viewed' "
            f"  AND properties.via = 'workshop' "
            f"  AND properties.from = '{webinar_id}' "
            f"  AND timestamp >= toDate('{ws_date}') "
            f"  AND timestamp <= toDate('{we_date}') + INTERVAL 1 DAY "
            "  AND properties.asset_name IS NOT NULL "
            "  AND properties.asset_name != '' "
            "GROUP BY resource_name "
            "ORDER BY cnt DESC "
            "LIMIT 20"
        )
        resource_rows = ph.get_hogql_query(api_key, project_id, resources_hogql)
        resources_used = [
            ResourceUsedRow(resource_name=str(r[0]), count=int(r[1]))
            for r in resource_rows
            if r[0]
        ]
    except Exception:
        pass  # degrade gracefully — empty list already set above

    result = WorkshopTimelineTrends(
        webinar_id=webinar_id,
        workshop_name=webinar["workshop_name"],
        start_datetime=start_dt.isoformat(),
        window_start=ws_date,
        window_end=we_date,
        weeks_before=weeks_before,
        weeks_after=weeks_after,
        days=days,
        registrations=_trend("registrations"),
        detail_views=_trend("detail_views"),
        video_watch_count=_trend("video_watch_count"),
        resource_views=_trend("resource_views"),
        video=video_stats,
        resources_used=resources_used,
    )
    ph._db_set(db, cache_key, result.model_dump())
    return result



@router.get("/library-coverage", response_model=LibraryCoverageData)
def get_library_coverage(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
) -> LibraryCoverageData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)

    pg = get_library_published_counts(db)

    date_clause = ph._hogql_date_clause(date_from, date_to)
    school_clause = ph._hogql_school_clause(sid)
    cycle_clause = ph._hogql_cycle_clause(cycle_name)

    # ONE HogQL: distinct viewed asset IDs and topic IDs
    hogql = (
        "SELECT "
        "  countDistinctIf(coalesce(properties.resource_id, properties.asset_id), "
        "    event IN ('resource_card_click', 'resource_viewed')), "
        "  countDistinctIf(properties.topic_id, "
        "    event IN ('topic_card_click', 'topic_viewed')) "
        "FROM events "
        f"WHERE {date_clause}{school_clause}{cycle_clause} "
        "AND event IN ('resource_card_click', 'resource_viewed', 'topic_card_click', 'topic_viewed')"
    )

    try:
        rows = ph.get_hogql_query(api_key, project_id, hogql)
        viewed_assets = int(rows[0][0]) if rows else 0
        viewed_topics = int(rows[0][1]) if rows else 0
    except Exception:
        viewed_assets = 0
        viewed_topics = 0

    pub_assets = pg["published_assets"]
    pub_topics = pg["published_topics"]

    coverage_pct = round(viewed_assets / pub_assets * 100, 2) if pub_assets else None
    topic_coverage_pct = round(viewed_topics / pub_topics * 100, 2) if pub_topics else None

    return LibraryCoverageData(
        published_assets=pub_assets,
        viewed_assets=viewed_assets,
        coverage_pct=coverage_pct,
        published_topics=pub_topics,
        viewed_topics=viewed_topics,
        topic_coverage_pct=topic_coverage_pct,
    )
