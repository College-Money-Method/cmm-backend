"""Admin triage endpoints for survey responses.

- GET   /api/v1/surveys/needs-review-count — badge count for the admin nav
- PATCH /api/v1/surveys/{id}/review        — mark one response reviewed / unreviewed
- POST  /api/v1/surveys/review-bulk        — same, for up to 200 responses

Review state is shared: any super_admin can review or undo, and everyone sees
who did it. The reviewer's email is stored on the row at review time so the
list endpoint never has to resolve auth users.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from sqlalchemy import func, select, update

from src.auth.deps import AdminDep
from src.auth.schemas import CurrentUser
from src.db.deps import DbDep
from src.surveys.actionable import needs_review_clause
from src.surveys.models import SurveyResponse
from src.surveys.schemas import (
    SurveyNeedsReviewCount,
    SurveyResponseOut,
    SurveyReviewBulkResult,
    SurveyReviewBulkUpdate,
    SurveyReviewUpdate,
)

router = APIRouter(prefix="/api/v1/surveys", tags=["surveys"])


def _review_values(admin: CurrentUser, reviewed: bool) -> dict:
    """Column values for marking reviewed (stamp who + when) or clearing it."""
    if not reviewed:
        return {"reviewed_at": None, "reviewed_by": None, "reviewed_by_email": None}
    return {
        "reviewed_at": datetime.now(timezone.utc),
        "reviewed_by": str(admin.user_id),
        "reviewed_by_email": admin.email,
    }


@router.get("/needs-review-count", response_model=SurveyNeedsReviewCount)
def get_needs_review_count(_admin: AdminDep, db: DbDep):
    """Admin: number of actionable responses no admin has reviewed yet."""
    count = db.scalar(select(func.count()).select_from(SurveyResponse).where(needs_review_clause()))
    return SurveyNeedsReviewCount(count=count or 0)


@router.patch("/{response_id}/review", response_model=SurveyResponseOut)
def set_response_reviewed(
    response_id: uuid.UUID,
    body: SurveyReviewUpdate,
    admin: AdminDep,
    db: DbDep,
):
    """Admin: mark one response reviewed, or clear the review."""
    row = db.get(SurveyResponse, response_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Survey response not found")
    if body.reviewed and row.reviewed_at is not None:
        return row  # already reviewed: keep who did it first
    for column, value in _review_values(admin, body.reviewed).items():
        setattr(row, column, value)
    db.commit()
    db.refresh(row)
    return row


@router.post("/review-bulk", response_model=SurveyReviewBulkResult)
def set_responses_reviewed_bulk(body: SurveyReviewBulkUpdate, admin: AdminDep, db: DbDep):
    """Admin: mark many responses reviewed/unreviewed. Unknown ids are ignored,
    and rows already reviewed keep their original reviewer."""
    stmt = update(SurveyResponse).where(SurveyResponse.id.in_(body.ids))
    if body.reviewed:
        stmt = stmt.where(SurveyResponse.reviewed_at.is_(None))
    result = db.execute(stmt.values(**_review_values(admin, body.reviewed)))
    db.commit()
    return SurveyReviewBulkResult(updated=result.rowcount or 0)
