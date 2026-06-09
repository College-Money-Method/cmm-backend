"""Counselor resource submission endpoints.

Mounted at /api/v1/content/submissions in src/main.py.
All endpoints require an authenticated user with role counselor or super_admin.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from src.auth.deps import CurrentUserDep
from src.content.models import ContentAsset
from src.content.schemas import SubmissionCreate, SubmissionOut, SubmissionUpdate
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/content/submissions", tags=["submissions"])

_ALLOWED_ROLES = {"counselor", "super_admin"}
_EDITABLE_STATUSES = {"draft", "rejected"}


def _require_counselor(user: CurrentUserDep) -> None:
    """Raise 403 if the caller is not a counselor or super_admin."""
    if user.role not in _ALLOWED_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Counselor or admin access required",
        )


def _load_submission(db: DbDep, submission_id: uuid.UUID) -> ContentAsset:
    stmt = (
        select(ContentAsset)
        .where(ContentAsset.id == submission_id)
        .options(selectinload(ContentAsset.asset_type))
    )
    obj = db.scalar(stmt)
    if not obj:
        raise HTTPException(status_code=404, detail="Submission not found")
    return obj


def _assert_owns(asset: ContentAsset, user_id: uuid.UUID) -> None:
    if asset.submitted_by_id != user_id:
        raise HTTPException(status_code=404, detail="Submission not found")


@router.post("/", response_model=SubmissionOut, status_code=status.HTTP_201_CREATED)
def create_submission(body: SubmissionCreate, user: CurrentUserDep, db: DbDep):
    """Create a draft submission on behalf of the authenticated counselor."""
    _require_counselor(user)

    asset_type_id: uuid.UUID | None = None
    if body.asset_type_id:
        try:
            asset_type_id = uuid.UUID(body.asset_type_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid asset_type_id")

    obj = ContentAsset(
        name=body.name,
        description=body.description,
        link=body.link,
        asset_type_id=asset_type_id,
        suggested_grades=body.suggested_grades,
        source="counselor",
        submitted_by_id=user.user_id,
        review_status="draft",
        status="draft",
        for_family=False,
        for_counselor=False,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _load_submission(db, obj.id)


@router.get("/", response_model=list[SubmissionOut])
def list_submissions(user: CurrentUserDep, db: DbDep):
    """List all submissions belonging to the authenticated counselor."""
    _require_counselor(user)

    stmt = (
        select(ContentAsset)
        .where(
            ContentAsset.source == "counselor",
            ContentAsset.submitted_by_id == user.user_id,
        )
        .options(selectinload(ContentAsset.asset_type))
        .order_by(ContentAsset.created_at.desc())
    )
    return db.scalars(stmt).all()


@router.patch("/{submission_id}", response_model=SubmissionOut)
def update_submission(
    submission_id: uuid.UUID, body: SubmissionUpdate, user: CurrentUserDep, db: DbDep
):
    """Update a draft or rejected submission. Returns 403 if already submitted."""
    _require_counselor(user)
    obj = _load_submission(db, submission_id)
    _assert_owns(obj, user.user_id)

    if obj.review_status not in _EDITABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot edit a submission with status '{obj.review_status}'",
        )

    data = body.model_dump(exclude_unset=True)
    # Handle asset_type_id string → UUID conversion
    if "asset_type_id" in data and data["asset_type_id"] is not None:
        try:
            data["asset_type_id"] = uuid.UUID(data["asset_type_id"])
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid asset_type_id")

    for k, v in data.items():
        setattr(obj, k, v)
    db.commit()
    return _load_submission(db, submission_id)


@router.delete("/{submission_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_submission(submission_id: uuid.UUID, user: CurrentUserDep, db: DbDep):
    """Delete a draft or rejected submission."""
    _require_counselor(user)
    obj = _load_submission(db, submission_id)
    _assert_owns(obj, user.user_id)

    if obj.review_status not in _EDITABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot delete a submission with status '{obj.review_status}'",
        )

    db.delete(obj)
    db.commit()


@router.post("/{submission_id}/submit", response_model=SubmissionOut)
def submit_for_review(
    submission_id: uuid.UUID,
    background_tasks: BackgroundTasks,
    user: CurrentUserDep,
    db: DbDep,
):
    """Submit a draft/rejected submission for review. Triggers AI pre-screen."""
    _require_counselor(user)
    obj = _load_submission(db, submission_id)
    _assert_owns(obj, user.user_id)

    if obj.review_status not in _EDITABLE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Cannot submit a submission with status '{obj.review_status}'",
        )

    obj.review_status = "ai_reviewing"
    db.commit()

    # Fire background AI pre-screen — import here to avoid circular imports
    from src.content.ai_review_task import ai_review_submission  # noqa: PLC0415
    from src.db.deps import get_db  # noqa: PLC0415

    def _run_ai_review() -> None:
        """Open a fresh DB session for the background task."""
        for _db in get_db():
            try:
                ai_review_submission(obj.id, _db)
            finally:
                pass  # get_db generator handles close

    background_tasks.add_task(_run_ai_review)

    return _load_submission(db, submission_id)
