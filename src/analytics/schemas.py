"""Pydantic schemas for analytics endpoints.

Existing shapes (OverviewData, WorkshopData, SearchData) are
FROZEN — the frontend depends on them exactly as-is.
New hub + admin schemas are appended below.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


# ── Shared primitives ─────────────────────────────────────────────────────────

class CachedAtMixin(BaseModel):
    """Adds `cached_at` — when the PostHog numbers in this response were actually
    read. None means they were computed for THIS request (i.e. "just now").

    The counselor-hub Refresh control reads it to label real data age and to
    disable itself until the entry is old enough to be worth re-querying; without
    it the client could only time from page-open, which made a 55-minute-old cache
    read "Last refreshed a few seconds ago". Populated from the oldest cache entry
    served during the request (query_cache.oldest_cache_hit)."""
    cached_at: datetime | None = None


class TrendMetric(BaseModel):
    total: int
    data: list[float]
    days: list[str]


class FunnelStep(BaseModel):
    name: str
    count: int


class TopBreakdown(BaseModel):
    label: str
    count: float  # float supports avg values (e.g. avg watch seconds)


# ── Existing endpoint response shapes (DO NOT CHANGE) ─────────────────────────

class OverviewData(BaseModel):
    # Split by surface — the same person can be active on both, and an
    # unqualified $pageview DAU also swept in CMM staff browsing /admin.
    dau: TrendMetric          # school Resource Center (/school/*) — families & students
    hub_dau: TrendMetric | None = None   # Counselor Hub (/hub/*) — counselors
    sign_ins: TrendMetric


class WorkshopData(BaseModel):
    watch_recordings: TrendMetric
    registrations_opened: TrendMetric
    registrations: TrendMetric
    # Both restricted to object_type='workshop' — video_view / video_session_end
    # also fire for topic, resource and welcome videos.
    top_videos: list[TopBreakdown]       # video_view count by object_name
    top_watchtime: list[TopBreakdown]    # video_session_end avg total_watch_seconds by object_name


class VideoBreakdownRow(BaseModel):
    name: str                              # workshop_name (videos are workshop recordings)
    view_count: int                        # video_view count (first play)
    avg_percent_watched: float | None = None


class ContentEngagementTotals(BaseModel):
    """Website-wide content-engagement totals for the Content-tab summary tiles —
    the true totals across ALL rows (not just the truncated top-N breakdowns)."""
    topic_engagement: int = 0   # topic_viewed count (visits to Topic pages)
    resources_used: int = 0     # resource_viewed count (all resources)
    video_views: int = 0        # video_view count (all videos, first play)


class RankedContentRow(BaseModel):
    """One row of a ranked content list.

    `id` is the entity's own UUID, which is what the counts are GROUPED BY — so a
    rename keeps one row (with its current name, read from Postgres) instead of
    splitting history, and two assets sharing a title stay separate instead of
    merging. It also lets the UI link the row to the entity's page. None → the
    entity is no longer in the library, or this list groups by name (see
    ContentBreakdownPage); either way the UI renders it unlinked."""
    id: str | None = None
    name: str
    count: int = 0


class ContentData(CachedAtMixin):
    """Number-only content breakdowns (no time-series charts) — kept lightweight:
    top videos (views + avg % watched), resources (views), topics (views)."""
    videos: list[VideoBreakdownRow] = []   # video_view count + video_session_end avg %, by object_name
    resources: list[RankedContentRow] = []  # resource_viewed by asset_id, named from Postgres
    topics: list[TopBreakdown] = []        # topic_viewed by topic_title
    # Aggregate totals for the "Content Engagement" summary tiles above the cards.
    totals: ContentEngagementTotals = Field(default_factory=ContentEngagementTotals)
    # Link base for the resource rows: /school/{school_slug}/resources/{id}.
    # None (admin viewing all schools) → rows render unlinked.
    school_slug: str | None = None


class ContentBreakdownPage(BaseModel):
    """One page of a full resources/topics ranked list (Content-tab "View all" popup).

    Resource rows carry `id` (grouped by asset id, same as ContentData.resources);
    topic rows are still grouped by title and leave it None."""
    rows: list[RankedContentRow] = []
    has_more: bool = False


# ── Topic-engagement tree (Grade → Goal → Topic) ───────────────────────────────

class TopicEngagementTopic(BaseModel):
    topic_id: str
    title: str
    slug: str                # links to /school/{school_slug}/topic/{slug}
    engagement: int = 0      # topic_viewed count for this topic
    video_views: int = 0     # video_view count (object_type='topic', object_id=topic_id)


class TopicEngagementGoal(BaseModel):
    goal_id: str
    name: str
    engagement: int = 0      # sum over this goal's topics
    video_views: int = 0
    topics: list[TopicEngagementTopic] = []


class TopicEngagementGrade(BaseModel):
    grade: int
    label: str                    # "9th Grade"
    page_title: str | None = None  # the grade page's own title ("Learn How Financial Aid Works")
    engagement: int = 0      # sum over this grade's goals
    video_views: int = 0
    goals: list[TopicEngagementGoal] = []


class TopicEngagementData(CachedAtMixin):
    """The school's published Grade → Goal → Topic hierarchy with per-topic
    engagement + video views, for the Content tab's Topic Engagement table.

    A goal may sit under several grades, so a topic can appear more than once;
    rollups count it under each grade it appears in (the tree mirrors the site's
    navigation rather than partitioning topics)."""
    school_slug: str | None = None   # None → topic titles render without links
    grades: list[TopicEngagementGrade] = []
    engagement: int = 0              # tree-wide totals (same duplication caveat)
    video_views: int = 0


class SearchData(BaseModel):
    # Site search = search_query (results page) + global_search_performed (dialog)
    searches: TrendMetric
    top_queries: list[TopBreakdown]
    # Resource Library keyword search (resource_library_searched event)
    library_searches: TrendMetric | None = None
    top_library_queries: list[TopBreakdown] | None = None


# ── New hub endpoint schemas ───────────────────────────────────────────────────

class WebinarDetail(BaseModel):
    webinar_id: str
    workshop_name: str
    start_datetime: str | None
    registered: int
    attended_live: int
    no_show: int
    joined_without_reg: int | None
    recording_views: int
    avg_percent_watched: float | None
    sequence_number: int | None = None
    detail_views: int = 0
    resource_views: int = 0


class WorkshopsDetailTotals(BaseModel):
    registered: int
    attended_live: int
    no_show: int
    recording_views: int
    detail_views: int = 0
    resource_views: int = 0


class SiteTotals(BaseModel):
    """Website-wide content totals (NOT workshop-scoped) for the summary cards.
    Still scoped by the counselor's school and the selected period."""
    visits: int = 0          # $pageview across the whole site
    video_views: int = 0     # video_view across all videos (first play)
    resource_views: int = 0  # resource_viewed across all resources


class WorkshopsDetailData(CachedAtMixin):
    webinars: list[WebinarDetail]
    totals: WorkshopsDetailTotals
    site_totals: SiteTotals = SiteTotals()


class ReachBenchmark(BaseModel):
    median_reach_pct: float
    peer_count: int
    above_median: bool


class ReachData(BaseModel):
    distinct_registrants: int
    enrollment: int | None
    reach_pct: float | None
    enrollment_range: str | None
    benchmark: ReachBenchmark | None


class PeakUsageCell(BaseModel):
    day: int    # 1=Mon .. 7=Sun (toDayOfWeek convention)
    hour: int
    count: int


class PeakUsageData(BaseModel):
    cells: list[PeakUsageCell]
    max_count: int


class LibraryCoverageData(BaseModel):
    published_assets: int
    viewed_assets: int
    coverage_pct: float | None
    published_topics: int
    viewed_topics: int
    topic_coverage_pct: float | None


# ── Admin endpoint schemas ────────────────────────────────────────────────────

class UpcomingWebinar(BaseModel):
    webinar_id: str
    workshop_name: str
    start_datetime: str | None
    registered: int


class PulseData(BaseModel):
    registrations_today: int
    registrations_this_week: int
    active_schools_count: int
    content_views_this_week: int
    upcoming_webinars: list[UpcomingWebinar]
    top_search_terms: list[TopBreakdown]
    top_resource_searches: list[TopBreakdown]


class StalledSchool(BaseModel):
    id: str
    name: str
    state: str | None
    enrollment_range: str | None
    created_at: str


class QuietSchool(BaseModel):
    id: str
    name: str
    state: str | None
    enrollment_range: str | None
    recent_regs: int
    prior_regs: int


class SchoolsHealthData(BaseModel):
    stalled_activations: list[StalledSchool]
    quiet_schools: list[QuietSchool]
    declining_schools: list[QuietSchool]


class EnrollmentMix(BaseModel):
    small: int
    medium: int
    large: int
    unknown: int


class BigPictureData(BaseModel):
    total_schools: int
    engaged_schools_period: int
    enrollment_mix: EnrollmentMix
    platform_dau: TrendMetric
    platform_registrations: TrendMetric


class WhatsWorkingData(BaseModel):
    top_resources: list[TopBreakdown]
    top_topics: list[TopBreakdown]
    top_workshops: list[TopBreakdown]
    zero_result_searches: list[TopBreakdown]


class EnrollmentBandStat(BaseModel):
    label: str
    count: int
    avg_reach_pct: float | None


class GeographicData(BaseModel):
    by_state: list[TopBreakdown]
    by_enrollment_band: list[EnrollmentBandStat]


# ── Workshop-timeline analytics schemas ───────────────────────────────────────

class ResourceUsedRow(BaseModel):
    """Single resource breakdown row for workshop-timeline resources_used."""
    resource_name: str
    count: int


class WorkshopTimelineTrends(CachedAtMixin):
    """Per-webinar windowed engagement CHART — adjustable window around
    start_datetime. Served by GET /workshop-timeline. The resources-used summary
    card is a SEPARATE payload (WorkshopEngagementCards) so the two can be
    fetched + rendered independently ("whatever comes first")."""
    webinar_id: str
    workshop_name: str
    start_datetime: str          # ISO-8601 UTC
    window_start: str            # YYYY-MM-DD
    window_end: str              # YYYY-MM-DD
    weeks_before: int            # applied window weeks before start
    weeks_after: int             # applied window weeks after start
    days: list[str]              # YYYY-MM-DD strings across full window
    registrations: TrendMetric   # DB WorkshopRegistration by registration_time (window)
    attendees: int = 0           # DB attended count (0 when workshop is outside window)
    detail_views: TrendMetric    # workshop_detail_view
    video_watch_count: TrendMetric  # video_view (first play = a "view")
    resource_views: TrendMetric  # resource_viewed via=workshop AND from=<webinar_id>


class WorkshopEngagementCards(CachedAtMixin):
    """Resources-used summary card for a single workshop within the timeline
    window. Served by GET /workshop-engagement — split from the chart trends so
    each streams to the UI on its own. (The aggregate video-stats card it also
    used to carry was dropped on 2026-07-31; workshop video views live in the
    Workshop Engagement tiles + timeline series.)"""
    webinar_id: str
    workshop_name: str
    resources_used: list[ResourceUsedRow]  # resource_viewed breakdown by asset_name


# ── Translation analytics (admin) ───────────────────────────────────────────

class TranslationTotals(BaseModel):
    cost_usd: float
    input_tokens: int
    output_tokens: int
    invocations: int
    cached_strings: int


class TranslationLocaleStat(BaseModel):
    locale: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    invocations: int


class TranslationContextStat(BaseModel):
    context: str
    cost_usd: float
    input_tokens: int
    output_tokens: int
    invocations: int


class TranslationDailyPoint(BaseModel):
    day: str  # ISO date (YYYY-MM-DD)
    cost_usd: float
    input_tokens: int
    output_tokens: int


class TranslationAnalytics(BaseModel):
    totals: TranslationTotals
    by_locale: list[TranslationLocaleStat]
    by_context: list[TranslationContextStat]
    daily: list[TranslationDailyPoint]
