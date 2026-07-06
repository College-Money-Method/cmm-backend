"""Pydantic schemas for analytics endpoints.

Existing shapes (OverviewData, WorkshopData, ContentData, SearchData) are
FROZEN — the frontend depends on them exactly as-is.
New hub + admin schemas are appended below.
"""

from __future__ import annotations

from pydantic import BaseModel


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


class ContentData(BaseModel):
    resource_clicks: TrendMetric
    topic_clicks: TrendMetric
    top_resources: list[TopBreakdown]   # resource_card_click by resource_name
    top_topics: list[TopBreakdown]      # topic_card_click by topic_title
    resource_views: TrendMetric | None = None        # resource_viewed (detail page)
    resource_link_opens: TrendMetric | None = None   # resource_detail_external_link_click
    top_pages: list[TopBreakdown] | None = None      # $pageview by $pathname


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


class WorkshopsDetailTotals(BaseModel):
    registered: int
    attended_live: int
    no_show: int
    recording_views: int


class WorkshopsDetailData(BaseModel):
    webinars: list[WebinarDetail]
    totals: WorkshopsDetailTotals


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
