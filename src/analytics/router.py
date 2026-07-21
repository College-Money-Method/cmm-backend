"""Analytics endpoints — proxy PostHog queries with school-level access control.

Existing 4 endpoints (overview/workshop/content/search) preserve exact response
shapes. New hub endpoints added below.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query
from src.analytics import posthog as ph
from src.analytics import posthog_batched as phb
from src.analytics.schemas import (
    ContentBreakdownPage,
    ContentData,
    ContentEngagementTotals,
    LibraryCoverageData,
    OverviewData,
    PeakUsageCell,
    PeakUsageData,
    ReachBenchmark,
    ReachData,
    ResourceUsedRow,
    SearchData,
    SiteTotals,
    TopBreakdown,
    TrendMetric,
    VideoBreakdownRow,
    WebinarDetail,
    WorkshopData,
    WorkshopEngagementCards,
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
    get_webinar_windowed_registrations,
    get_webinars_for_school_in_range,
    get_workshops_detail_totals,
)
from src.auth.deps import CounselorDep
from src.config import settings
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

logger = logging.getLogger(__name__)


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
    # Number-only content page — ONE PostHog round trip, no time-series (lightweight).
    # Videos need two aggregations (view count + avg % watched) on the same breakdown;
    # a wider limit on the pct branch ensures every top-viewed video has its avg merged.
    # Videos: show all (only a handful of workshop recordings per cycle). Resources
    # & topics: top 10 in the card; the "View all" popup paginates via /content-breakdown.
    breakdowns = phb.get_batched_breakdowns(api_key, project_id, [
        {"key": "video_views", "event": "video_session_end", "prop": "workshop_name", "limit": 50},
        {"key": "video_pct", "event": "video_session_end", "prop": "workshop_name",
         "math": "avg", "math_prop": "percent_watched", "limit": 100},
        {"key": "resources", "event": "resource_viewed", "prop": "asset_name", "limit": 10},
        {"key": "topics", "event": "topic_viewed", "prop": "topic_title", "limit": 10},
    ], **opts)
    pct_by_name = {r.label: r.count for r in breakdowns["video_pct"]}
    videos = [
        VideoBreakdownRow(name=r.label, view_count=int(r.count), avg_percent_watched=pct_by_name.get(r.label))
        for r in breakdowns["video_views"]
    ]

    # Aggregate totals for the "Content Engagement" summary tiles — the TRUE
    # totals across all rows (the breakdowns above are truncated to top-N).
    # One extra lightweight round trip: a single count-by-event aggregate.
    totals_hogql = (
        "SELECT "
        "countIf(event = 'topic_viewed') AS topic_engagement, "
        "countIf(event = 'resource_viewed') AS resources_used, "
        "countIf(event = 'video_session_end') AS video_views "
        "FROM events "
        f"WHERE {ph._hogql_date_clause(date_from, date_to)}{ph._hogql_school_clause(sid)}{ph._hogql_cycle_clause(cycle_name)} "
        "AND event IN ('topic_viewed', 'resource_viewed', 'video_session_end')"
    )
    totals = ContentEngagementTotals()
    try:
        rows = ph.get_hogql_query(api_key, project_id, totals_hogql)
        if rows:
            r0 = rows[0]
            totals = ContentEngagementTotals(
                topic_engagement=int(r0[0] or 0),
                resources_used=int(r0[1] or 0),
                video_views=int(r0[2] or 0),
            )
    except Exception:
        logger.warning("PostHog error computing content totals", exc_info=True)

    return ContentData(
        videos=videos,
        resources=breakdowns["resources"],
        topics=breakdowns["topics"],
        totals=totals,
    )


# kind → (event, breakdown property) for the Content-tab "View all" popup.
_CONTENT_BREAKDOWN_KINDS = {
    "resources": ("resource_viewed", "asset_name"),
    "topics": ("topic_viewed", "topic_title"),
}


@router.get("/content-breakdown", response_model=ContentBreakdownPage)
def get_content_breakdown(
    current_user: CounselorDep,
    kind: str = Query(..., description="resources | topics"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
) -> ContentBreakdownPage:
    """Paginated full ranked list for the Content-tab resources/topics popup.
    Fetches limit+1 rows so the client knows whether another page exists."""
    if kind not in _CONTENT_BREAKDOWN_KINDS:
        raise HTTPException(status_code=400, detail="kind must be 'resources' or 'topics'")
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    event, prop = _CONTENT_BREAKDOWN_KINDS[kind]

    # The HogQL Query API rejects bare OFFSET, so fetch the top `offset+limit+1`
    # rows (breakdown lists are small) and slice the page in Python — matching the
    # LIMIT-only style of the working batched-breakdown query.
    where = f"{ph._hogql_date_clause(date_from, date_to)}{ph._hogql_school_clause(sid)}{ph._hogql_cycle_clause(cycle_name)}"
    fetch_n = offset + limit + 1
    hogql = (
        f"SELECT toString(properties.{prop}) AS label, count() AS c "
        f"FROM events WHERE event = '{event}' AND {where} "
        f"AND isNotNull(properties.{prop}) "
        f"GROUP BY label ORDER BY c DESC LIMIT {fetch_n}"
    )
    all_rows: list[TopBreakdown] = []
    try:
        result = ph.get_hogql_query(api_key, project_id, hogql)
        all_rows = [
            TopBreakdown(label=str(r[0]), count=float(r[1]))
            for r in result
            if r[0] and str(r[0]) not in ("", "Other") and not str(r[0]).startswith("$$_posthog")
        ]
    except Exception:
        logger.warning("PostHog error in get_content_breakdown(%s)", kind, exc_info=True)
    page = all_rows[offset : offset + limit]
    return ContentBreakdownPage(rows=page, has_more=len(all_rows) > offset + limit)


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


def _unpack_workshops_detail_ph(cached: dict) -> tuple[SiteTotals, dict, dict, dict]:
    """Rebuild the (site_totals, rec, detail, res) tuple from a cached payload."""
    site = SiteTotals(**cached["site"])
    rec = {
        k: (int(v[0]), (float(v[1]) if v[1] is not None else None))
        for k, v in cached["rec"].items()
    }
    return site, rec, dict(cached["detail"]), dict(cached["res"])


def _query_workshops_detail_ph(
    api_key: str,
    project_id: str,
    db,
    sid: str,
    date_from: str,
    date_to: str | None,
    cycle_name: str | None,
) -> tuple[SiteTotals, dict, dict, dict]:
    """Fetch the four PostHog aggregates powering workshops-detail:
    website-wide site totals + per-webinar recording / detail-view / resource maps.

    The four HogQL queries are independent, so they run CONCURRENTLY (each is a
    blocking HTTP round trip) instead of sequentially — this alone cuts the cold
    latency from ~4×round-trip to ~1×. Results are stored in the durable query
    cache (stale-on-error), so repeat loads of the Overview / Live Workshops tabs
    return instantly rather than re-hitting PostHog (the ~8s bug).
    """
    cache_key = ph._key(fn="workshops_detail_ph", school_id=sid, df=date_from, dt=date_to, cyc=cycle_name)
    if (cached := ph._db_get(db, cache_key, sid)) is not None:
        return _unpack_workshops_detail_ph(cached)

    date_clause = ph._hogql_date_clause(date_from, date_to)
    school_clause = ph._hogql_school_clause(sid)
    cycle_clause = ph._hogql_cycle_clause(cycle_name)

    # Website-wide content totals for the summary tiles — NOT restricted to
    # workshops (still scoped by this school + selected period).
    site_hogql = (
        "SELECT "
        "countIf(event = '$pageview') AS visits, "
        "countIf(event = 'video_session_end') AS video_views, "
        "countIf(event = 'resource_viewed') AS resource_views "
        "FROM events "
        f"WHERE {date_clause}{school_clause}{cycle_clause} "
        "AND event IN ('$pageview', 'video_session_end', 'resource_viewed')"
    )
    # Recording views + avg % watched per webinar_id (video_session_end).
    hogql_rec = (
        "SELECT properties.webinar_id, count(), avg(toFloat(ifNull(properties.percent_watched, '0'))) "
        "FROM events "
        f"WHERE event = 'video_session_end' AND {date_clause}{school_clause}{cycle_clause} "
        "AND isNotNull(properties.webinar_id) "
        "GROUP BY properties.webinar_id"
    )
    # Detail views per webinar_id (workshop_detail_view).
    hogql_detail = (
        "SELECT properties.webinar_id, count() "
        "FROM events "
        f"WHERE event = 'workshop_detail_view' AND {date_clause}{school_clause}{cycle_clause} "
        "AND isNotNull(properties.webinar_id) "
        "GROUP BY properties.webinar_id"
    )
    # Resource views per webinar_id (resource_viewed WHERE via='workshop'); the
    # origin webinar is carried in properties.from.
    hogql_res = (
        "SELECT properties.from, count() "
        "FROM events "
        f"WHERE event = 'resource_viewed' AND {date_clause}{school_clause}{cycle_clause} "
        "AND properties.via = 'workshop' "
        "AND isNotNull(properties.from) "
        "GROUP BY properties.from"
    )

    def _run(q: str):
        try:
            return ph.get_hogql_query(api_key, project_id, q)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=4) as ex:
        site_rows, rec_rows, detail_rows, res_rows = ex.map(
            _run, [site_hogql, hogql_rec, hogql_detail, hogql_res]
        )

    # Total PostHog outage → serve stale if we have it; never cache the empties.
    if site_rows is None and rec_rows is None and detail_rows is None and res_rows is None:
        stale = ph._db_get_stale(db, cache_key)
        if stale is not None:
            return _unpack_workshops_detail_ph(stale)
        return SiteTotals(), {}, {}, {}

    # Parse each result defensively — a malformed row must degrade to empty for
    # that metric, not fail the whole request (mirrors the original per-query guards).
    site_totals = SiteTotals()
    try:
        if site_rows:
            r0 = site_rows[0]
            site_totals = SiteTotals(visits=int(r0[0]), video_views=int(r0[1]), resource_views=int(r0[2]))
    except Exception:
        pass
    try:
        ph_map = {
            str(r[0]): (int(r[1]), float(r[2]) if r[2] is not None else None)
            for r in (rec_rows or []) if r[0]
        }
    except Exception:
        ph_map = {}
    try:
        detail_map = {str(r[0]): int(r[1]) for r in (detail_rows or []) if r[0]}
    except Exception:
        detail_map = {}
    try:
        res_map = {str(r[0]): int(r[1]) for r in (res_rows or []) if r[0]}
    except Exception:
        res_map = {}

    ph._db_set(db, cache_key, {
        "site": {
            "visits": site_totals.visits,
            "video_views": site_totals.video_views,
            "resource_views": site_totals.resource_views,
        },
        "rec": {k: [v[0], v[1]] for k, v in ph_map.items()},
        "detail": detail_map,
        "res": res_map,
    })
    return site_totals, ph_map, detail_map, res_map


@router.get("/workshops-detail", response_model=WorkshopsDetailData)
def get_workshops_detail(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
    cycle_id: uuid.UUID | None = Query(default=None),
    range_numbers: bool = Query(default=False),
) -> WorkshopsDetailData:
    """range_numbers=true (with cycle_id + a date range): keep ALL cycle
    webinars as rows but scope the NUMBERS to the range — registrations by
    registration date, attendees zeroed for webinars outside the range, and the
    PostHog engagement metrics already follow date_from/date_to."""
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    if not sid:
        raise HTTPException(status_code=400, detail="school_id is required for workshops-detail")

    school_uuid = uuid.UUID(sid)
    # cycle_id filters webinars by their cycle (families browse cycle content
    # outside the cycle's calendar dates); date range is the fallback
    rows = get_webinars_for_school_in_range(
        db, school_uuid, date_from, date_to, cycle_id=cycle_id, range_numbers=range_numbers
    )

    # Site totals + per-webinar PostHog maps — the four HogQL queries run in
    # parallel and are cached (see helper), so this is the fast path on repeat loads.
    site_totals, ph_map, detail_map, res_map = _query_workshops_detail_ph(
        api_key, project_id, db, sid, date_from, date_to, cycle_name
    )

    if rows:
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
        site_totals=site_totals,
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


# Hard cap on a custom workshop window (~15 months) — bounds the zero-filled
# daily series and keeps the PostHog scan cheap.
_MAX_WINDOW_DAYS = 460


def _resolve_webinar_window(
    db,
    webinar_id: str,
    weeks_before: int,
    weeks_after: int,
    date_from: str | None = None,
    date_to: str | None = None,
) -> tuple[dict, datetime, date, date]:
    """Validate webinar_id, load the webinar, and compute its analysis window.

    An explicit date_from/date_to pair (YYYY-MM-DD, from the UI's calendar range
    picker) takes precedence; otherwise the window falls back to ±weeks around
    start_datetime. Shared by the split workshop-timeline (chart) and
    workshop-engagement (cards) endpoints so both scope events to the identical
    window. Raises HTTPException on bad id / missing webinar / missing
    start_datetime / malformed dates.
    """
    try:
        webinar_uuid = uuid.UUID(webinar_id)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"webinar_id must be a valid UUID: {webinar_id!r}")
    phb._validate_webinar_id(webinar_id)  # belt-and-suspenders: rejects non-UUID chars

    webinar = get_webinar_by_id(db, webinar_uuid)
    if webinar is None:
        raise HTTPException(status_code=404, detail=f"Webinar {webinar_id!r} not found")
    if webinar["start_datetime"] is None:
        raise HTTPException(
            status_code=422,
            detail=f"Webinar {webinar_id!r} has no start_datetime; cannot compute timeline window",
        )
    start_dt = webinar["start_datetime"]

    if date_from and date_to:
        try:
            window_start = date.fromisoformat(date_from)
            window_end = date.fromisoformat(date_to)
        except ValueError:
            raise HTTPException(status_code=422, detail="date_from/date_to must be YYYY-MM-DD")
        if window_end < window_start:
            raise HTTPException(status_code=422, detail="date_to must be on or after date_from")
        if (window_end - window_start).days + 1 > _MAX_WINDOW_DAYS:
            raise HTTPException(status_code=422, detail=f"window too large (max {_MAX_WINDOW_DAYS} days)")
    else:
        window_start = (start_dt - timedelta(days=weeks_before * 7)).date()
        window_end = (start_dt + timedelta(days=weeks_after * 7)).date()
    return webinar, start_dt, window_start, window_end


@router.get("/workshop-timeline", response_model=WorkshopTimelineTrends)
def get_workshop_timeline(
    current_user: CounselorDep,
    db: DbDep,
    webinar_id: str = Query(...),
    school_id: str | None = Query(default=None),
    weeks_before: int = Query(default=4, ge=0, le=52),
    weeks_after: int = Query(default=4, ge=0, le=52),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> WorkshopTimelineTrends:
    """Per-webinar windowed engagement CHART — the 4 daily TrendMetric series.

    webinar_id = internal webinar UUID (matches PostHog properties.webinar_id).
    Window = explicit [date_from, date_to] (YYYY-MM-DD, from the UI's calendar
    range picker) when both are given; else [start − weeks_before*7d, start +
    weeks_after*7d]. The video/resources summary cards are served separately by
    /workshop-engagement so the (fast, single-query) chart doesn't wait on them.
    """
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)

    # v2: registrations + attendees now come from the DB (registration_time),
    # not PostHog events — bump the cache fn so stale event-based entries are ignored.
    cache_key = ph._key(
        fn="workshop_timeline_v2",
        webinar_id=webinar_id,
        school_id=sid,
        weeks_before=weeks_before,
        weeks_after=weeks_after,
        date_from=date_from,
        date_to=date_to,
    )
    if (cached := ph._db_get(db, cache_key, sid)) is not None:
        return WorkshopTimelineTrends.model_validate(cached)

    webinar, start_dt, window_start, window_end = _resolve_webinar_window(
        db, webinar_id, weeks_before, weeks_after, date_from, date_to
    )
    total_days = (window_end - window_start).days + 1  # inclusive of both ends
    days = [(window_start + timedelta(days=i)).isoformat() for i in range(total_days)]

    webinar_windows: list[phb.WebinarWindow] = [
        {"webinar_id": webinar_id, "window_start": window_start, "window_end": window_end}
    ]

    # Registrations come from the DB (below), NOT PostHog. The remaining three
    # series are PostHog engagement events.
    # resource_viewed carries the origin workshop in properties.from (its
    # webinar_id is empty), so match on `from` and add only the via='workshop'
    # filter — the windowed helper matches from=<webinar_id> per window.
    event_specs: list[phb.EventSpec] = [
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
        raw = phb.get_windowed_trends_by_webinar(
            api_key, project_id, webinar_windows, event_specs, sid, None
        )
    except Exception as exc:
        stale = ph._db_get_stale(db, cache_key)
        if stale:
            return WorkshopTimelineTrends.model_validate(stale)
        raise HTTPException(status_code=503, detail="PostHog unavailable") from exc

    # Build each PostHog TrendMetric with zero-fill
    def _trend(event_key: str) -> TrendMetric:
        pairs = raw.get(event_key, {}).get(webinar_id, [])
        day_count: dict[str, int] = {}
        for d, cnt in pairs:
            day_count[d] = day_count.get(d, 0) + cnt
        data = [float(day_count.get(d, 0)) for d in days]
        return TrendMetric(total=int(sum(data)), data=data, days=days)

    # Registrations + attendees from the DB (authoritative), windowed by
    # registration_time. Scoped to the school when one is resolved (counselor
    # view); super_admin viewing all schools passes school_id=None.
    sid_uuid = uuid.UUID(sid) if sid else None
    reg_by_day, attendees = get_webinar_windowed_registrations(
        db, uuid.UUID(webinar_id), sid_uuid, window_start, window_end, start_dt
    )
    reg_data = [float(reg_by_day.get(d, 0)) for d in days]
    registrations = TrendMetric(total=int(sum(reg_data)), data=reg_data, days=days)

    result = WorkshopTimelineTrends(
        webinar_id=webinar_id,
        workshop_name=webinar["workshop_name"],
        start_datetime=start_dt.isoformat(),
        window_start=window_start.isoformat(),
        window_end=window_end.isoformat(),
        weeks_before=weeks_before,
        weeks_after=weeks_after,
        days=days,
        registrations=registrations,
        attendees=attendees,
        detail_views=_trend("detail_views"),
        video_watch_count=_trend("video_watch_count"),
        resource_views=_trend("resource_views"),
    )
    ph._db_set(db, cache_key, result.model_dump())
    return result


@router.get("/workshop-engagement", response_model=WorkshopEngagementCards)
def get_workshop_engagement(
    current_user: CounselorDep,
    db: DbDep,
    webinar_id: str = Query(...),
    school_id: str | None = Query(default=None),
    weeks_before: int = Query(default=4, ge=0, le=52),
    weeks_after: int = Query(default=4, ge=0, le=52),
    date_from: str | None = Query(default=None),
    date_to: str | None = Query(default=None),
) -> WorkshopEngagementCards:
    """Video-engagement + resources-used summary CARDS for a workshop's window.

    Window resolution mirrors /workshop-timeline (explicit date range wins over
    ±weeks). Split from it so the two PostHog reads here (video stats + resource
    breakdown) stream to the UI independently of the chart — the client renders
    whichever payload resolves first. The two reads run concurrently.
    """
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)

    cache_key = ph._key(
        fn="workshop_engagement",
        webinar_id=webinar_id,
        school_id=sid,
        weeks_before=weeks_before,
        weeks_after=weeks_after,
        date_from=date_from,
        date_to=date_to,
    )
    if (cached := ph._db_get(db, cache_key, sid)) is not None:
        return WorkshopEngagementCards.model_validate(cached)

    webinar, _start_dt, window_start, window_end = _resolve_webinar_window(
        db, webinar_id, weeks_before, weeks_after, date_from, date_to
    )
    ws_date = window_start.isoformat()
    we_date = window_end.isoformat()

    # Video aggregate stats (powers the "Video engagement" card).
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
    # Resources-used breakdown (powers the "Resources used" card).
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

    def _run(query: str):
        try:
            return ph.get_hogql_query(api_key, project_id, query)
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=2) as ex:
        f_video = ex.submit(_run, video_hogql)
        f_res = ex.submit(_run, resources_hogql)
        video_rows = f_video.result()
        resource_rows = f_res.result()

    # Total outage on both reads → serve stale if we have it, else degrade to
    # empties WITHOUT caching (so a transient failure doesn't poison the cache).
    if video_rows is None and resource_rows is None:
        stale = ph._db_get_stale(db, cache_key)
        if stale:
            return WorkshopEngagementCards.model_validate(stale)
        return WorkshopEngagementCards(
            webinar_id=webinar_id,
            workshop_name=webinar["workshop_name"],
            video=WorkshopVideoStats(total_plays=0, total_minutes_watched=0, avg_percent_watched=None),
            resources_used=[],
        )

    # -- Video aggregate stats — degrade gracefully to zeros on parse failure. --
    video_stats = WorkshopVideoStats(total_plays=0, total_minutes_watched=0, avg_percent_watched=None)
    try:
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
        pass

    # -- Resources-used breakdown — degrade gracefully to empty on parse failure. --
    resources_used: list[ResourceUsedRow] = []
    try:
        resources_used = [
            ResourceUsedRow(resource_name=str(r[0]), count=int(r[1]))
            for r in (resource_rows or [])
            if r[0]
        ]
    except Exception:
        pass

    result = WorkshopEngagementCards(
        webinar_id=webinar_id,
        workshop_name=webinar["workshop_name"],
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
