"""Pydantic schemas for analytics endpoints.

Existing shapes (OverviewData, WorkshopData, SearchData) are
FROZEN — the frontend depends on them exactly as-is.
New hub + admin schemas are appended below.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── Shared primitives ─────────────────────────────────────────────────────────

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
    dau: TrendMetric
    sign_ins: TrendMetric


class WorkshopData(BaseModel):
    watch_recordings: TrendMetric
    registrations_opened: TrendMetric
    registrations: TrendMetric
    top_videos: list[TopBreakdown]       # video_session_end count by workshop_name
    top_watchtime: list[TopBreakdown]    # video_session_end avg total_watch_seconds by workshop_name
    # recording_progress counts per milestone_pct (10..100), milestone order —
    # shows where viewers stop watching recordings
    milestone_dropoff: list[TopBreakdown] = []


class VideoBreakdownRow(BaseModel):
    name: str                              # workshop_name (videos are workshop recordings)
    view_count: int                        # video_session_end count
    avg_percent_watched: float | None = None


class ContentEngagementTotals(BaseModel):
    """Website-wide content-engagement totals for the Content-tab summary tiles —
    the true totals across ALL rows (not just the truncated top-N breakdowns)."""
    topic_engagement: int = 0   # topic_viewed count (visits to Topic pages)
    resources_used: int = 0     # resource_viewed count (all resources)
    video_views: int = 0        # video_session_end count (all videos)


class ContentData(BaseModel):
    """Number-only content breakdowns (no time-series charts) — kept lightweight:
    top videos (views + avg % watched), resources (views), topics (views)."""
    videos: list[VideoBreakdownRow] = []   # video_session_end by workshop_name
    resources: list[TopBreakdown] = []     # resource_viewed by asset_name
    topics: list[TopBreakdown] = []        # topic_viewed by topic_title
    # Aggregate totals for the "Content Engagement" summary tiles above the cards.
    totals: ContentEngagementTotals = Field(default_factory=ContentEngagementTotals)


class ContentBreakdownPage(BaseModel):
    """One page of a full resources/topics ranked list (Content-tab "View all" popup)."""
    rows: list[TopBreakdown] = []
    has_more: bool = False


class SearchData(BaseModel):
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
    video_views: int = 0     # video_session_end across all videos
    resource_views: int = 0  # resource_viewed across all resources


class WorkshopsDetailData(BaseModel):
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

class WorkshopVideoStats(BaseModel):
    """Aggregate video stats for a single workshop within the timeline window."""
    total_plays: int
    total_minutes_watched: int
    avg_percent_watched: float | None


class ResourceUsedRow(BaseModel):
    """Single resource breakdown row for workshop-timeline resources_used."""
    resource_name: str
    count: int


class WorkshopTimelineTrends(BaseModel):
    """Per-webinar windowed engagement CHART — adjustable window around
    start_datetime. Served by GET /workshop-timeline. The video/resources summary
    cards are a SEPARATE payload (WorkshopEngagementCards) so the two can be
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
    video_watch_count: TrendMetric  # video_session_end
    resource_views: TrendMetric  # resource_viewed via=workshop AND from=<webinar_id>


class WorkshopEngagementCards(BaseModel):
    """Video-engagement + resources-used summary cards for a single workshop
    within the timeline window. Served by GET /workshop-engagement — split from
    the chart trends so each streams to the UI on its own."""
    webinar_id: str
    workshop_name: str
    video: WorkshopVideoStats    # aggregate video stats within window
    resources_used: list[ResourceUsedRow]  # resource_viewed breakdown by asset_name
