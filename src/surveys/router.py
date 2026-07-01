"""Surveys API router.

Three endpoints:
- POST /api/v1/surveys       — public, submit a survey response (optional auth)
- GET  /api/v1/surveys       — admin only, list responses with filters + pagination
- GET  /api/v1/surveys/summary — admin only, aggregated stats per page_type
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.auth.deps import AdminDep
from src.db.client import get_supabase
from src.db.deps import DbDep
from src.surveys.models import SurveyResponse
from src.surveys.schemas import (
    SurveyListResponse,
    SurveyPageTypeSummary,
    SurveyResponseCreate,
    SurveyResponseOut,
    SurveySummaryResponse,
)

router = APIRouter(prefix="/api/v1/surveys", tags=["surveys"])

_optional_bearer = HTTPBearer(auto_error=False)


@router.post("", response_model=SurveyResponseOut, status_code=201)
def submit_survey(
    body: SurveyResponseCreate,
    db: DbDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_optional_bearer)] = None,
    supabase=Depends(get_supabase),
):
    """Public: submit a survey response.

    Accepts an optional Bearer token — if valid, the authenticated user_id is
    stored alongside the posthog_distinct_id to link the response to a known user.
    Anonymous school users (password-gated, not logged in) submit without a token.
    """
    user_id: str | None = None
    if credentials:
        try:
            user_resp = supabase.auth.get_user(credentials.credentials)
            if user_resp and user_resp.user:
                user_id = str(user_resp.user.id)
        except Exception:
            pass  # treat as anonymous — still store posthog_distinct_id

    row = SurveyResponse(
        id=uuid.uuid4(),
        page_type=body.page_type,
        page_url=body.page_url,
        resource_id=body.resource_id,
        resource_name=body.resource_name,
        school_id=body.school_id,
        question_type=body.question_type,
        question_text=body.question_text,
        rating_thumbs=body.rating_thumbs,
        rating_stars=body.rating_stars,
        comment=body.comment,
        posthog_distinct_id=body.posthog_distinct_id,
        user_id=user_id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


@router.get("/summary", response_model=SurveySummaryResponse)
def get_surveys_summary(_admin: AdminDep, db: DbDep):
    """Admin: aggregated stats per page_type for the admin dashboard."""
    rows: list[SurveyResponse] = db.scalars(select(SurveyResponse)).all()

    total = len(rows)
    by_type: dict[str, list[SurveyResponse]] = {}
    for r in rows:
        by_type.setdefault(r.page_type, []).append(r)

    summaries: list[SurveyPageTypeSummary] = []
    for page_type, items in sorted(by_type.items()):
        thumbs = [i for i in items if i.rating_thumbs is not None]
        stars = [i for i in items if i.rating_stars is not None]
        thumbs_up = sum(1 for i in thumbs if i.rating_thumbs is True)
        thumbs_down = len(thumbs) - thumbs_up
        summaries.append(
            SurveyPageTypeSummary(
                page_type=page_type,
                total=len(items),
                thumbs_up=thumbs_up,
                thumbs_down=thumbs_down,
                thumbs_up_pct=round(thumbs_up / len(thumbs) * 100, 1) if thumbs else None,
                avg_stars=round(sum(i.rating_stars for i in stars) / len(stars), 2) if stars else None,
                with_comment=sum(1 for i in items if i.comment),
            )
        )

    return SurveySummaryResponse(total=total, by_page_type=summaries)


@router.get("", response_model=SurveyListResponse)
def list_surveys(
    _admin: AdminDep,
    db: DbDep,
    page_type: str | None = Query(None),
    question_type: str | None = Query(None),
    school_id: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
):
    """Admin: list survey responses with optional filters and pagination."""
    stmt = select(SurveyResponse).order_by(SurveyResponse.created_at.desc())

    if page_type:
        stmt = stmt.where(SurveyResponse.page_type == page_type)
    if question_type:
        stmt = stmt.where(SurveyResponse.question_type == question_type)
    if school_id:
        stmt = stmt.where(SurveyResponse.school_id == school_id)
    if date_from:
        stmt = stmt.where(SurveyResponse.created_at >= date_from)
    if date_to:
        stmt = stmt.where(SurveyResponse.created_at <= date_to)

    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    items = db.scalars(stmt.offset(skip).limit(limit)).all()

    return SurveyListResponse(items=list(items), total=total or 0, skip=skip, limit=limit)
