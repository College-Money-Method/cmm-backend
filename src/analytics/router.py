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
    SearchData,
    TrendMetric,
    WebinarDetail,
    WorkshopData,
    WorkshopsDetailData,
    WorkshopsDetailTotals,
    WorkshopTimelineTrends,
    WorkshopTimelineEntry,
    WorkshopsTimelineOverviewData,
)
from src.analytics.postgres_queries import (
    get_library_published_counts,
    get_reach_benchmark,
    get_reach_data,
    get_webinar_by_id,
    get_webinars_for_school_by_cycle_name,
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
        # ONE PostHog HogQL: recording views + avg % watched per webinar_id
        date_clause = ph._hogql_date_clause(date_from, date_to)
        school_clause = ph._hogql_school_clause(sid)
        cycle_clause = ph._hogql_cycle_clause(cycle_name)
        hogql = (
            "SELECT properties.webinar_id, count(), avg(toFloat(ifNull(properties.percent_watched, '0'))) "
            "FROM events "
            f"WHERE event = 'video_session_end' AND {date_clause}{school_clause}{cycle_clause} "
            "AND isNotNull(properties.webinar_id) "
            "GROUP BY properties.webinar_id"
        )
        try:
            ph_rows = ph.get_hogql_query(api_key, project_id, hogql)
            ph_map: dict[str, tuple[int, float | None]] = {
                str(r[0]): (int(r[1]), float(r[2]) if r[2] is not None else None)
                for r in ph_rows if r[0]
            }
        except Exception:
            ph_map = {}

        for row in rows:
            vid = row["webinar_id"]
            if vid in ph_map:
                row["recording_views"] = ph_map[vid][0]
                row["avg_percent_watched"] = ph_map[vid][1]

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
) -> WorkshopTimelineTrends:
    """Per-webinar windowed engagement: 30d before → 14d after start_datetime.

    webinar_id = internal webinar UUID (matches PostHog properties.webinar_id).
    Returns 4 daily TrendMetric series zero-filled across a 44-day window.
    """
    api_key, project_id = _check_configured()
    # Validate webinar_id as UUID — PostHog properties.webinar_id is the internal UUID
    try:
        webinar_uuid = uuid.UUID(webinar_id)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"webinar_id must be a valid UUID: {webinar_id!r}")
    phb._validate_webinar_id(webinar_id)  # belt-and-suspenders: rejects non-UUID chars
    sid = _resolve_school(current_user, school_id)

    cache_key = ph._key(fn="workshop_timeline", webinar_id=webinar_id, school_id=sid)
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

    # -30d to +14d inclusive = 45 days (30 before + day-0 + 14 after)
    start_dt = webinar["start_datetime"]
    window_start: date = (start_dt - timedelta(days=30)).date()
    window_end: date = (start_dt + timedelta(days=14)).date()
    days = [(window_start + timedelta(days=i)).isoformat() for i in range(45)]

    webinar_windows: list[phb.WebinarWindow] = [
        {"webinar_id": webinar_id, "window_start": window_start, "window_end": window_end}
    ]

    # Resource views need per-webinar via/from filter — inline extra_filter
    resource_extra = f" AND properties.via = 'workshop' AND properties.from = '{webinar_id}'"
    event_specs: list[phb.EventSpec] = [
        {"key": "registrations", "event": "workshop_registration_complete"},
        {"key": "detail_views", "event": "workshop_detail_view"},
        {"key": "video_watch_count", "event": "video_session_end"},
        {"key": "resource_views", "event": "resource_viewed", "extra_filter": resource_extra},
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

    result = WorkshopTimelineTrends(
        webinar_id=webinar_id,
        workshop_name=webinar["workshop_name"],
        start_datetime=start_dt.isoformat(),
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        days=days,
        registrations=_trend("registrations"),
        detail_views=_trend("detail_views"),
        video_watch_count=_trend("video_watch_count"),
        resource_views=_trend("resource_views"),
    )
    ph._db_set(db, cache_key, result.model_dump())
    return result


@router.get("/workshops-timeline-overview", response_model=WorkshopsTimelineOverviewData)
def get_workshops_timeline_overview(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    cycle_name: str = Query(...),
) -> WorkshopsTimelineOverviewData:
    """All-workshop aggregate + per-workshop headline for the overview tab.

    cycle_name (required) bounds the webinar set — resolves to webinars via
    cycle.name → Cycle.id → webinars for school+cycle.
    Aggregate series use relative days ("-30" .. "0" .. "+14").
    """
    api_key, project_id = _check_configured()
    ph._validate_cycle_name(cycle_name)  # validate before cache key + HogQL use
    sid = _resolve_school(current_user, school_id)
    if not sid:
        raise HTTPException(status_code=400, detail="school_id required for workshops-timeline-overview")

    school_uuid = uuid.UUID(sid)
    cache_key = ph._key(fn="workshops_timeline_overview", school_id=sid, cycle_name=cycle_name)
    if (cached := ph._db_get(db, cache_key, sid)) is not None:
        return WorkshopsTimelineOverviewData.model_validate(cached)

    # Resolve cycle_name → webinars for this school
    webinar_rows = get_webinars_for_school_by_cycle_name(db, school_uuid, cycle_name)

    if len(webinar_rows) > phb._MAX_WEBINARS_PER_REQUEST:
        import logging as _log
        _log.getLogger(__name__).warning(
            "workshops-timeline-overview: %d webinars for school %s cycle %r; capped at %d",
            len(webinar_rows), sid, cycle_name, phb._MAX_WEBINARS_PER_REQUEST,
        )
        webinar_rows = webinar_rows[:phb._MAX_WEBINARS_PER_REQUEST]

    # Relative day labels: -30 to +14 (inclusive = 45 points)
    relative_days = [str(i) for i in range(-30, 15)]

    # Separate webinars with/without start_datetime
    valid_rows = [r for r in webinar_rows if r["start_datetime"] is not None]
    null_rows = [r for r in webinar_rows if r["start_datetime"] is None]

    if not valid_rows:
        # All webinars lack start_datetime — return empty aggregates
        empty_trend = _empty_relative_trend(relative_days)
        workshops = [_null_entry(r) for r in webinar_rows]
        result = WorkshopsTimelineOverviewData(
            workshops=workshops,
            aggregate_registrations=empty_trend,
            aggregate_detail_views=empty_trend,
            aggregate_video_watch_count=empty_trend,
        )
        ph._db_set(db, cache_key, result.model_dump())
        return result

    # Build windowed query inputs for valid webinars only
    webinar_windows: list[phb.WebinarWindow] = []
    window_map: dict[str, tuple[date, date]] = {}  # webinar_id → (ws, we)
    for row in valid_rows:
        start_dt = row["start_datetime"]
        ws = (start_dt - timedelta(days=30)).date()
        we = (start_dt + timedelta(days=14)).date()
        vid = row["webinar_id"]
        webinar_windows.append({"webinar_id": vid, "window_start": ws, "window_end": we})
        window_map[vid] = (ws, we)

    # resource_views need per-webinar via/from filters — one event_spec per webinar
    # For simplicity: we issue the base events in one batch query, then resource_views
    # in a separate per-webinar batch. Both are ONE HogQL each (UNION ALL).
    base_specs: list[phb.EventSpec] = [
        {"key": "registrations", "event": "workshop_registration_complete"},
        {"key": "detail_views", "event": "workshop_detail_view"},
        {"key": "video_watch_count", "event": "video_session_end"},
    ]

    try:
        raw = phb.get_windowed_trends_by_webinar(
            api_key, project_id, webinar_windows, base_specs, sid, db
        )
    except Exception as exc:
        stale = ph._db_get_stale(db, cache_key)
        if stale:
            return WorkshopsTimelineOverviewData.model_validate(stale)
        raise HTTPException(status_code=503, detail="PostHog unavailable") from exc

    # Build aggregate relative-day buckets: {relative_day_str: {event_key: count}}
    # Note: resource_views not included in WorkshopTimelineEntry (overview headline).
    agg_reg: dict[str, int] = {d: 0 for d in relative_days}
    agg_detail: dict[str, int] = {d: 0 for d in relative_days}
    agg_video: dict[str, int] = {d: 0 for d in relative_days}

    for row in valid_rows:
        _vid = row["webinar_id"]
        _start_dt = row["start_datetime"]

        for event_key, agg in (
            ("registrations", agg_reg),
            ("detail_views", agg_detail),
            ("video_watch_count", agg_video),
        ):
            for day_str, cnt in raw.get(event_key, {}).get(_vid, []):
                try:
                    d = date.fromisoformat(day_str)
                    rel_key = str((d - _start_dt.date()).days)
                    if rel_key in agg:
                        agg[rel_key] += cnt
                except Exception:
                    pass

    def _to_relative_trend(agg: dict[str, int]) -> TrendMetric:
        data = [float(agg.get(d, 0)) for d in relative_days]
        return TrendMetric(total=int(sum(data)), data=data, days=relative_days)

    def _build_entry(row: dict) -> WorkshopTimelineEntry:
        vid = row["webinar_id"]
        ws_we = window_map.get(vid)
        if ws_we is None:
            return _null_entry(row)
        ws, we = ws_we

        # Totals from raw
        reg_total = sum(cnt for _, cnt in raw.get("registrations", {}).get(vid, []))
        detail_total = sum(cnt for _, cnt in raw.get("detail_views", {}).get(vid, []))
        video_total = sum(cnt for _, cnt in raw.get("video_watch_count", {}).get(vid, []))

        start_dt = row["start_datetime"]
        return WorkshopTimelineEntry(
            webinar_id=vid,
            workshop_name=row["workshop_name"],
            start_datetime=start_dt.isoformat() if start_dt else None,
            window_start=ws.isoformat(),
            window_end=we.isoformat(),
            registered=reg_total,
            detail_views=detail_total,
            video_watch_count=video_total,
        )

    workshops = [_build_entry(r) for r in valid_rows] + [_null_entry(r) for r in null_rows]
    # Sort by start_datetime: valid (ascending) then null
    workshops.sort(key=lambda e: (e.start_datetime is None, e.start_datetime or ""))

    result = WorkshopsTimelineOverviewData(
        workshops=workshops,
        aggregate_registrations=_to_relative_trend(agg_reg),
        aggregate_detail_views=_to_relative_trend(agg_detail),
        aggregate_video_watch_count=_to_relative_trend(agg_video),
    )
    ph._db_set(db, cache_key, result.model_dump())
    return result


def _empty_relative_trend(relative_days: list[str]) -> TrendMetric:
    return TrendMetric(total=0, data=[0.0] * len(relative_days), days=relative_days)


def _null_entry(row: dict) -> WorkshopTimelineEntry:
    return WorkshopTimelineEntry(
        webinar_id=row["webinar_id"],
        workshop_name=row["workshop_name"],
        start_datetime=None,
        window_start=None,
        window_end=None,
        registered=0,
        detail_views=0,
        video_watch_count=0,
    )


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
