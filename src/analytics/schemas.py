from pydantic import BaseModel


class TrendMetric(BaseModel):
    total: int
    data: list[float]
    days: list[str]


class FunnelStep(BaseModel):
    name: str
    count: int


class TopBreakdown(BaseModel):
    label: str
    count: float  # float to support avg values (e.g. avg watch seconds)


class OverviewData(BaseModel):
    dau: TrendMetric
    sign_ins: TrendMetric


class WorkshopData(BaseModel):
    watch_recordings: TrendMetric
    registrations_opened: TrendMetric
    registrations: TrendMetric
    funnel: list[FunnelStep]
    top_videos: list[TopBreakdown]       # video_session_end count by workshop_name
    top_watchtime: list[TopBreakdown]    # video_session_end avg total_watch_seconds by workshop_name


class ContentData(BaseModel):
    resource_clicks: TrendMetric
    topic_clicks: TrendMetric
    top_resources: list[TopBreakdown]   # resource_card_click by resource_name
    top_topics: list[TopBreakdown]      # topic_card_click by topic_title


class SearchData(BaseModel):
    searches: TrendMetric
    top_queries: list[TopBreakdown]
