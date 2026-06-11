"""Analytics endpoints — proxy PostHog queries with school-level access control."""

from fastapi import APIRouter, HTTPException, Query

from src.analytics import posthog as ph
from src.analytics.schemas import ContentData, OverviewData, SearchData, WorkshopData
from src.auth.deps import CounselorDep
from src.config import settings

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


@router.get("/overview", response_model=OverviewData)
def get_overview(
    current_user: CounselorDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
) -> OverviewData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to)
    return OverviewData(
        dau=ph.get_trend(api_key, project_id, "$pageview", math="dau", prop_type="person", **opts),
        sign_ins=ph.get_trend(api_key, project_id, "user_signed_in", **opts),
    )


@router.get("/workshop", response_model=WorkshopData)
def get_workshop(
    current_user: CounselorDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
) -> WorkshopData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to)
    return WorkshopData(
        watch_recordings=ph.get_trend(api_key, project_id, "workshop_watch_recording", **opts),
        registrations_opened=ph.get_trend(api_key, project_id, "workshop_register_open", **opts),
        registrations=ph.get_trend(api_key, project_id, "workshop_registration_complete", **opts),
        funnel=ph.get_funnel(api_key, project_id, "workshop_register_open", "workshop_registration_complete", **opts),
        top_videos=ph.get_top_breakdown(api_key, project_id, "video_session_end", "workshop_name", limit=10, **opts),
        top_watchtime=ph.get_top_breakdown(
            api_key, project_id, "video_session_end", "workshop_name",
            math="avg", math_property="total_watch_seconds", limit=10, **opts,
        ),
    )


@router.get("/content", response_model=ContentData)
def get_content(
    current_user: CounselorDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
) -> ContentData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to)
    return ContentData(
        resource_clicks=ph.get_trend(api_key, project_id, "resource_card_click", **opts),
        topic_clicks=ph.get_trend(api_key, project_id, "topic_card_click", **opts),
        top_resources=ph.get_top_breakdown(api_key, project_id, "resource_card_click", "resource_name", limit=10, **opts),
        top_topics=ph.get_top_breakdown(api_key, project_id, "topic_card_click", "topic_title", limit=10, **opts),
    )


@router.get("/search", response_model=SearchData)
def get_search(
    current_user: CounselorDep,
    school_id: str | None = Query(default=None),
    date_from: str = Query(default="-30d"),
    date_to: str | None = Query(default=None),
) -> SearchData:
    api_key, project_id = _check_configured()
    sid = _resolve_school(current_user, school_id)
    opts = dict(school_id=sid, date_from=date_from, date_to=date_to)
    return SearchData(
        searches=ph.get_trend(api_key, project_id, "search_query", **opts),
        top_queries=ph.get_top_breakdown(api_key, project_id, "search_query", "query", **opts),
    )
