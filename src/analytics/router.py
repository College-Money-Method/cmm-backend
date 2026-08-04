"""Analytics endpoints — proxy PostHog queries with school-level access control.

Existing 4 endpoints (overview/workshop/content/search) preserve exact response
shapes. New hub endpoints added below.
"""

from __future__ import annotations

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from typing import TypeVar

from fastapi import APIRouter, HTTPException, Query
from src.analytics import posthog as ph
from src.analytics import posthog_batched as phb
from src.analytics.query_cache import single_flight
from src.analytics.schemas import (
    CachedAtMixin,
    ContentBreakdownPage,
    ContentData,
    ContentEngagementTotals,
    LibraryCoverageData,
    OverviewData,
    PeakUsageCell,
    PeakUsageData,
    ReachBenchmark,
    RankedContentRow,
    ReachData,
    ResourceUsedRow,
    SearchData,
    SiteTotals,
    TopBreakdown,
    TopicEngagementData,
    TopicEngagementGoal,
    TopicEngagementGrade,
    TopicEngagementTopic,
    TrendMetric,
    VideoBreakdownRow,
    HubWebinarItem,
    WebinarDetail,
    WorkshopData,
    WorkshopEngagementCards,
    WorkshopsDetailData,
    WorkshopsDetailTotals,
    WorkshopTimelineTrends,
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
from src.analytics.resource_breakdown_queries import (
    get_school_slug,
    resolve_asset_rows,
    resolve_video_rows,
)
from src.analytics.topic_engagement_queries import get_school_topic_tree, get_topic_metrics
from src.auth.deps import CounselorDep
from src.config import settings
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

logger = logging.getLogger(__name__)

# video_view / video_session_end carry object_type ∈ workshop|topic|resource|welcome
_WORKSHOP_VIDEO_ONLY = " AND properties.object_type = 'workshop'"
_TOPIC_VIDEO_ONLY = " AND properties.object_type = 'topic'"
# Resource-embedded + welcome videos — the "Other Videos" card, i.e. everything
# not covered by the topic ("Topic Video Views") or workshop breakdowns.
_OTHER_VIDEO_ONLY = " AND properties.object_type IN ('resource', 'welcome')"


def _resolve_school(current_user: CounselorDep, school_id_param: str | None) -> str | None:
    """Admins can filter by any school or see all; counselors are locked to their school."""
    if current_user.role == "super_admin":
        return school_id_param or None
    return str(current_user.school_id) if current_user.school_id else None


def _check_configured() -> tuple[str, str]:
    if not settings.posthog_api_key or not settings.posthog_project_id:
        raise HTTPException(status_code=503, detail="PostHog analytics not configured")
    return settings.posthog_api_key, settings.posthog_project_id


_M = TypeVar("_M", bound=CachedAtMixin)


def _stamp_cache_age(model: _M, db) -> _M:
    """Set `cached_at` to the oldest cache entry served this request.

    Used on the early cache-hit / stale-serve returns, where the payload was
    validated straight out of the cache and so carries the *previous* request's
    value (None). Endpoints that assemble their model at a single point pass
    cached_at= directly instead."""
    return model.model_copy(update={"cached_at": ph.oldest_cache_hit(db)})


# ── Existing endpoints (response shapes MUST NOT change) ──────────────────────

@router.get("/overview", response_model=OverviewData)
def get_overview(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
    refresh: bool = Query(default=False),
) -> OverviewData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to, cycle_name=cycle_name, db=db, force_refresh=refresh)
    # ONE PostHog round trip for all series.
    # Note: school scoping is now the event-property super-prop for DAU too
    # (was person-property) — first-visit pageviews before registration are excluded.
    # DAU is split by surface: an unqualified $pageview DAU silently mixed
    # families on the Resource Center, counselors in the Hub, and CMM staff in
    # /admin, so the single number answered nobody's question.
    trends = phb.get_batched_trends(api_key, project_id, [
        {"key": "dau", "event": "$pageview", "math": "dau", "extra_filter": ph.SURFACE_RESOURCE_CENTER},
        {"key": "hub_dau", "event": "$pageview", "math": "dau", "extra_filter": ph.SURFACE_HUB},
        {"key": "sign_ins", "event": "user_signed_in"},
    ], **opts)
    return OverviewData(dau=trends["dau"], hub_dau=trends["hub_dau"], sign_ins=trends["sign_ins"])


@router.get("/workshop", response_model=WorkshopData)
def get_workshop(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
    refresh: bool = Query(default=False),
) -> WorkshopData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to, cycle_name=cycle_name, db=db, force_refresh=refresh)
    # TWO PostHog round trips total (was 6 sequential calls)
    trends = phb.get_batched_trends(api_key, project_id, [
        {"key": "watch_recordings", "event": "workshop_watch_recording"},
        {"key": "registrations_opened", "event": "workshop_register_open"},
        {"key": "registrations", "event": "workshop_registration_complete"},
    ], **opts)
    # video_view / video_session_end fire for EVERY embedded video — topic
    # videos, resource videos and the site welcome video included — so both
    # workshop breakdowns must filter on object_type or they rank non-workshops.
    breakdowns = phb.get_batched_breakdowns(api_key, project_id, [
        {"key": "top_videos", "event": "video_view", "prop": "object_name", "limit": 10,
         "extra_filter": _WORKSHOP_VIDEO_ONLY},
        {"key": "top_watchtime", "event": "video_session_end", "prop": "object_name",
         "math": "avg", "math_prop": "total_watch_seconds", "limit": 10,
         "extra_filter": _WORKSHOP_VIDEO_ONLY},
    ], **opts)
    return WorkshopData(
        watch_recordings=trends["watch_recordings"],
        registrations_opened=trends["registrations_opened"],
        registrations=trends["registrations"],
        top_videos=breakdowns["top_videos"],
        top_watchtime=breakdowns["top_watchtime"],
    )


@router.get("/content", response_model=ContentData)
def get_content(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
    refresh: bool = Query(default=False),
) -> ContentData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to, cycle_name=cycle_name, db=db, force_refresh=refresh)
    # Number-only content page — ONE PostHog round trip, no time-series (lightweight).
    # Videos need two aggregations (view count + avg % watched) on the same breakdown;
    # a wider limit on the pct branch ensures every top-viewed video has its avg merged.
    # Videos: show all (only a handful of workshop recordings per cycle). Resources
    # & topics: top 10 in the card; the "View all" popup paginates via /content-breakdown.
    breakdowns = phb.get_batched_breakdowns(api_key, project_id, [
        {"key": "video_views", "event": "video_view", "prop": "object_name", "limit": 50,
         "extra_filter": _TOPIC_VIDEO_ONLY},
        {"key": "video_pct", "event": "video_session_end", "prop": "object_name",
         "math": "avg", "math_prop": "percent_watched", "limit": 100,
         "extra_filter": _TOPIC_VIDEO_ONLY},
        # Grouped by asset_id, NOT asset_name: names collide across assets and
        # change over time. resolve_asset_rows() supplies the current names.
        {"key": "resources", "event": "resource_viewed", "prop": "asset_id", "limit": 10},
        {"key": "topics", "event": "topic_viewed", "prop": "topic_title", "limit": 10},
        # Resource-embedded + welcome videos for the "Other Videos" card. Grouped
        # by object_id (NOT object_name) so resource videos carry their asset id
        # and link like the Top Resources card — resolve_video_rows names them.
        {"key": "other_videos", "event": "video_view", "prop": "object_id", "limit": 10,
         "extra_filter": _OTHER_VIDEO_ONLY},
    ], **opts)
    pct_by_name = {r.label: r.count for r in breakdowns["video_pct"]}
    videos = [
        VideoBreakdownRow(name=r.label, view_count=int(r.count), avg_percent_watched=pct_by_name.get(r.label))
        for r in breakdowns["video_views"]
    ]

    # Aggregate totals for the "Content Engagement" summary tiles — the TRUE
    # totals across all rows (the breakdowns above are truncated to top-N).
    # One extra lightweight round trip: a single count-by-event aggregate.
    # video_views counts ONLY Topic Page videos (object_type = 'topic') — the
    # "Topic Video Views" tile is topic-scoped, not all Resource Center videos.
    # (Resource + welcome video plays are surfaced as the named "Other Videos"
    # list, not a tile, so they're not aggregated here.)
    totals_hogql = (
        "SELECT "
        "countIf(event = 'topic_viewed') AS topic_engagement, "
        "countIf(event = 'resource_viewed') AS resources_used, "
        "countIf(event = 'video_view' AND properties.object_type = 'topic') AS video_views "
        "FROM events "
        f"WHERE {ph._hogql_date_clause(date_from, date_to)}{ph._hogql_school_clause(sid)}{ph._hogql_cycle_clause(cycle_name)} "
        "AND event IN ('topic_viewed', 'resource_viewed', 'video_view')"
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
        resources=resolve_asset_rows(db, breakdowns["resources"]),
        topics=breakdowns["topics"],
        other_videos=resolve_video_rows(db, breakdowns["other_videos"]),
        totals=totals,
        school_slug=get_school_slug(db, sid),
        cached_at=ph.oldest_cache_hit(db),
    )


# kind → (event, breakdown property, resolve ids to current Postgres names)
# for the Content-tab "View all" popup. Resources group by id so the popup
# ranks identically to the card (and can link each row); the topics kind is no
# longer reachable from the UI and stays name-grouped.
_CONTENT_BREAKDOWN_KINDS = {
    "resources": ("resource_viewed", "asset_id", True),
    "topics": ("topic_viewed", "topic_title", False),
}


@router.get("/content-breakdown", response_model=ContentBreakdownPage)
def get_content_breakdown(
    current_user: CounselorDep,
    db: DbDep,
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
    event, prop, resolve_names = _CONTENT_BREAKDOWN_KINDS[kind]

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
    # Resolve only the page — one small IN query instead of the whole ranking.
    rows = (
        resolve_asset_rows(db, page)
        if resolve_names
        else [RankedContentRow(name=r.label, count=int(r.count)) for r in page]
    )
    return ContentBreakdownPage(rows=rows, has_more=len(all_rows) > offset + limit)


@router.get("/topic-engagement", response_model=TopicEngagementData)
def get_topic_engagement(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
    refresh: bool = Query(default=False),
) -> TopicEngagementData:
    """Grade → Goal → Topic tree with per-topic engagement + video views.

    The hierarchy comes from Postgres (the school's grade set, published topics
    only — cheap, always current); the numbers come from ONE cached HogQL query
    keyed on the topic ids. Topics with no activity are kept, showing zeros, so
    the counselor sees full coverage rather than only what was visited.
    """
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)

    school_slug, grades = get_school_topic_tree(db, sid)
    topic_ids = [t["topic_id"] for g in grades for gl in g["goals"] for t in gl["topics"]]
    metrics = get_topic_metrics(
        api_key, project_id, topic_ids,
        school_id=sid, date_from=date_from, date_to=date_to,
        cycle_name=cycle_name, db=db, force_refresh=refresh,
    )

    out_grades: list[TopicEngagementGrade] = []
    tree_eng = tree_vid = 0
    for g in grades:
        out_goals: list[TopicEngagementGoal] = []
        g_eng = g_vid = 0
        for gl in g["goals"]:
            topics = [
                TopicEngagementTopic(
                    topic_id=t["topic_id"], title=t["title"], slug=t["slug"],
                    engagement=metrics.get(t["topic_id"], {}).get("engagement", 0),
                    video_views=metrics.get(t["topic_id"], {}).get("video_views", 0),
                )
                for t in gl["topics"]
            ]
            gl_eng = sum(t.engagement for t in topics)
            gl_vid = sum(t.video_views for t in topics)
            out_goals.append(TopicEngagementGoal(
                goal_id=gl["goal_id"], name=gl["name"],
                engagement=gl_eng, video_views=gl_vid, topics=topics,
            ))
            g_eng += gl_eng
            g_vid += gl_vid
        out_grades.append(TopicEngagementGrade(
            grade=g["grade"], label=g["label"], page_title=g["page_title"],
            engagement=g_eng, video_views=g_vid, goals=out_goals,
        ))
        tree_eng += g_eng
        tree_vid += g_vid

    return TopicEngagementData(
        school_slug=school_slug, grades=out_grades,
        engagement=tree_eng, video_views=tree_vid,
        cached_at=ph.oldest_cache_hit(db),
    )


@router.get("/search", response_model=SearchData)
def get_search(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
    cycle_name: str | None = Query(default=None),
    refresh: bool = Query(default=False),
) -> SearchData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to, cycle_name=cycle_name, db=db, force_refresh=refresh)
    # TWO PostHog round trips total (was 4 sequential calls)
    trends = phb.get_batched_trends(api_key, project_id, [
        {"key": "searches", "event": ph.SITE_SEARCH_EVENTS},
        {"key": "library_searches", "event": "resource_library_searched"},
    ], **opts)
    breakdowns = phb.get_batched_breakdowns(api_key, project_id, [
        {"key": "top_queries", "event": ph.SITE_SEARCH_EVENTS, "prop": "query", "limit": 8},
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
    force: bool = False,
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
    if (cached := ph._db_get(db, cache_key, sid, force=force)) is not None:
        return _unpack_workshops_detail_ph(cached)

    with single_flight(db, cache_key):
        # Re-check: a request we waited on may have just filled the cache.
        # (force still bypasses this, so Refresh keeps recomputing.)
        if (cached := ph._db_get(db, cache_key, sid, force=force)) is not None:
            return _unpack_workshops_detail_ph(cached)

        date_clause = ph._hogql_date_clause(date_from, date_to)
        school_clause = ph._hogql_school_clause(sid)
        cycle_clause = ph._hogql_cycle_clause(cycle_name)

        # Website-wide content totals for the summary tiles — NOT restricted to
        # workshops (still scoped by this school + selected period).
        site_hogql = (
            "SELECT "
            "countIf(event = '$pageview') AS visits, "
            "countIf(event = 'video_view') AS video_views, "
            "countIf(event = 'resource_viewed') AS resource_views "
            "FROM events "
            f"WHERE {date_clause}{school_clause}{cycle_clause} "
            "AND event IN ('$pageview', 'video_view', 'resource_viewed')"
        )
        # Recording VIEWS per object_id (video_view — the "view" = first play metric).
        # Video events key on properties.object_id (workshop videos → webinar UUID).
        hogql_rec_views = (
            "SELECT properties.object_id, count() "
            "FROM events "
            f"WHERE event = 'video_view' AND {date_clause}{school_clause}{cycle_clause} "
            "AND isNotNull(properties.object_id) "
            "GROUP BY properties.object_id"
        )
        # Avg % watched per object_id (video_session_end — watch-quality stat, kept
        # separate because only the session-end summary carries percent_watched).
        hogql_rec_pct = (
            "SELECT properties.object_id, avg(toFloat(ifNull(properties.percent_watched, '0'))) "
            "FROM events "
            f"WHERE event = 'video_session_end' AND {date_clause}{school_clause}{cycle_clause} "
            "AND isNotNull(properties.object_id) "
            "GROUP BY properties.object_id"
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

        with ThreadPoolExecutor(max_workers=5) as ex:
            site_rows, rec_view_rows, rec_pct_rows, detail_rows, res_rows = ex.map(
                _run, [site_hogql, hogql_rec_views, hogql_rec_pct, hogql_detail, hogql_res]
            )

        # Outage handling is deferred until AFTER parsing (see the guard before
        # _db_set): a PARTIAL failure must not overwrite a complete cache entry.

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
            # Merge the two per-webinar reads: view count (video_view) + avg %
            # watched (video_session_end), keyed by object_id (= webinar UUID for
            # workshop videos, so it aligns with the DB webinar rows). Shape stays
            # (view_count, avg_pct) so downstream consumers are unchanged.
            view_map = {str(r[0]): int(r[1]) for r in (rec_view_rows or []) if r[0]}
            pct_map = {
                str(r[0]): (float(r[1]) if r[1] is not None else None)
                for r in (rec_pct_rows or []) if r[0]
            }
            ph_map = {
                wid: (view_map.get(wid, 0), pct_map.get(wid))
                for wid in (view_map.keys() | pct_map.keys())
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

        # Total OR partial PostHog outage: at least one sub-query failed. Never
        # overwrite a previously-complete entry with partial/zeroed data — critical
        # on a forced refresh (which bypassed the read and would otherwise clobber
        # good data for the whole TTL). Prefer the stale-but-complete entry; if none
        # exists, return the partial result WITHOUT caching it.
        if (site_rows is None or rec_view_rows is None or rec_pct_rows is None
                or detail_rows is None or res_rows is None):
            stale = ph._db_get_stale(db, cache_key)
            if stale is not None:
                return _unpack_workshops_detail_ph(stale)
            return site_totals, ph_map, detail_map, res_map

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
    refresh: bool = Query(default=False),
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
        api_key, project_id, db, sid, date_from, date_to, cycle_name, force=refresh
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
        cached_at=ph.oldest_cache_hit(db),
    )


@router.get("/hub/webinars", response_model=list[HubWebinarItem])
def list_hub_webinars(
    current_user: CounselorDep,
    db: DbDep,
    school_id: str | None = Query(default=None),
    cycle_id: uuid.UUID | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
) -> list[HubWebinarItem]:
    """Lightweight webinar list for the hub workshop selector.

    Returns the SAME school+cycle scoped set as /workshops-detail (via
    PortalMapping) but WITHOUT the PostHog aggregates — a fast DB-only call the
    client caches in localStorage (webinars are scheduled once per cycle). When
    cycle_id is given the range is ignored for row selection (webinars are scoped
    by their cycle)."""
    sid = _resolve_school(current_user, school_id)
    if not sid:
        raise HTTPException(status_code=400, detail="school_id is required for hub webinars")
    rows = get_webinars_for_school_in_range(
        db, uuid.UUID(sid), date_from, date_to, cycle_id=cycle_id
    )
    return [
        HubWebinarItem(
            webinar_id=r["webinar_id"],
            workshop_name=r["workshop_name"],
            start_datetime=r["start_datetime"],
            sequence_number=r.get("sequence_number"),
        )
        for r in rows
    ]


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
    refresh: bool = Query(default=False),
) -> PeakUsageData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)

    cache_key = ph._key(fn="peak_usage", school_id=sid, df=date_from, dt=date_to, cyc=cycle_name)
    if (cached := ph._db_get(db, cache_key, sid, force=refresh)) is not None:
        return PeakUsageData.model_validate(cached)

    with single_flight(db, cache_key):
        # Re-check: a request we waited on may have just filled the cache.
        # (refresh still bypasses this, so Refresh keeps recomputing.)
        if (cached := ph._db_get(db, cache_key, sid, force=refresh)) is not None:
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
    refresh: bool = Query(default=False),
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
    if (cached := ph._db_get(db, cache_key, sid, force=refresh)) is not None:
        return _stamp_cache_age(WorkshopTimelineTrends.model_validate(cached), db)

    with single_flight(db, cache_key):
        # Re-check: a request we waited on may have just filled the cache.
        # (refresh still bypasses this, so Refresh keeps recomputing.)
        if (cached := ph._db_get(db, cache_key, sid, force=refresh)) is not None:
            return _stamp_cache_age(WorkshopTimelineTrends.model_validate(cached), db)

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
            # video_view keys the webinar on properties.object_id (workshop → webinar id)
            {"key": "video_watch_count", "event": "video_view", "match_prop": "object_id"},
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
                return _stamp_cache_age(WorkshopTimelineTrends.model_validate(stale), db)
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
    refresh: bool = Query(default=False),
) -> WorkshopEngagementCards:
    """Resources-used summary CARD for a workshop's window.

    Window resolution mirrors /workshop-timeline (explicit date range wins over
    ±weeks). Split from it so this PostHog read streams to the UI independently
    of the chart — the client renders whichever payload resolves first.
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
    if (cached := ph._db_get(db, cache_key, sid, force=refresh)) is not None:
        return _stamp_cache_age(WorkshopEngagementCards.model_validate(cached), db)

    with single_flight(db, cache_key):
        # Re-check: a request we waited on may have just filled the cache.
        # (refresh still bypasses this, so Refresh keeps recomputing.)
        if (cached := ph._db_get(db, cache_key, sid, force=refresh)) is not None:
            return _stamp_cache_age(WorkshopEngagementCards.model_validate(cached), db)

        webinar, _start_dt, window_start, window_end = _resolve_webinar_window(
            db, webinar_id, weeks_before, weeks_after, date_from, date_to
        )
        ws_date = window_start.isoformat()
        we_date = window_end.isoformat()

        # School scoping: CMM webinars are SHARED across schools, so events for one
        # webinar_id span many schools. Without this clause the card aggregates every
        # school's activity — inflating the counts vs. the school-scoped Overview
        # card and timeline tile (empty string for super_admin viewing all schools).
        school_clause = ph._hogql_school_clause(sid)

        # Resources-used breakdown (powers the "Resources used" card).
        resources_hogql = (
            "SELECT "
            "  properties.asset_name AS resource_name, "
            "  count() AS cnt "
            "FROM events "
            f"WHERE event = 'resource_viewed' "
            f"  AND properties.via = 'workshop' "
            f"  AND properties.from = '{webinar_id}'{school_clause} "
            f"  AND timestamp >= toDate('{ws_date}') "
            f"  AND timestamp <= toDate('{we_date}') + INTERVAL 1 DAY "
            "  AND properties.asset_name IS NOT NULL "
            "  AND properties.asset_name != '' "
            "GROUP BY resource_name "
            "ORDER BY cnt DESC "
            "LIMIT 20"
        )

        try:
            resource_rows = ph.get_hogql_query(api_key, project_id, resources_hogql)
        except Exception:
            resource_rows = None

        # Outage handling is deferred until AFTER parsing (see the guard before
        # _db_set) so a failed read never overwrites a complete cache entry —
        # critical on a forced refresh.

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
            resources_used=resources_used,
        )

        # Outage: the read failed. Don't overwrite a complete entry with empty data
        # (esp. on a forced refresh). Prefer the stale-but-complete entry; else
        # return the empty result WITHOUT caching it.
        if resource_rows is None:
            stale = ph._db_get_stale(db, cache_key)
            if stale:
                return _stamp_cache_age(WorkshopEngagementCards.model_validate(stale), db)
            return result

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
