"""Survey config CRUD router.

Public:
    GET /api/v1/survey-configs          — active configs consumed by the feedback widget

Admin only:
    GET    /api/v1/survey-configs/admin  — all configs (active + inactive)
    POST   /api/v1/survey-configs        — create; auto-deactivates existing active for same page_type
    PATCH  /api/v1/survey-configs/{id}   — update name/text/trigger/is_active (question_type is immutable)
    DELETE /api/v1/survey-configs/{id}   — hard delete
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from sqlalchemy import select

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.surveys.models import SurveyConfig
from src.surveys.schemas import SurveyConfigCreate, SurveyConfigOut, SurveyConfigUpdate

router = APIRouter(prefix="/api/v1/survey-configs", tags=["survey-configs"])


@router.get("", response_model=list[SurveyConfigOut])
def list_active_configs(db: DbDep):
    """Public: returns all active survey configs — consumed by the feedback widget."""
    return db.scalars(select(SurveyConfig).where(SurveyConfig.is_active.is_(True))).all()


@router.get("/admin", response_model=list[SurveyConfigOut])
def list_all_configs(_admin: AdminDep, db: DbDep):
    """Admin: all configs including inactive, newest first."""
    return db.scalars(select(SurveyConfig).order_by(SurveyConfig.created_at.desc())).all()


@router.post("", response_model=SurveyConfigOut, status_code=201)
def create_config(body: SurveyConfigCreate, _admin: AdminDep, db: DbDep):
    """Admin: create a survey config.

    Enforces one-active-per-page_type: any existing active config for the same
    page_type is deactivated before the new one is inserted.
    """
    existing = db.scalar(
        select(SurveyConfig).where(
            SurveyConfig.page_type == body.page_type,
            SurveyConfig.is_active.is_(True),
        )
    )
    if existing:
        existing.is_active = False

    config = SurveyConfig(
        id=uuid.uuid4(),
        name=body.name,
        page_type=body.page_type,
        question_text=body.question_text,
        question_type=body.question_type,
        comment_prompt=body.comment_prompt,
        trigger_type=body.trigger_type,
        trigger_value=body.trigger_value,
        is_active=True,
    )
    db.add(config)
    db.commit()
    db.refresh(config)
    return config


@router.patch("/{config_id}", response_model=SurveyConfigOut)
def update_config(config_id: uuid.UUID, body: SurveyConfigUpdate, _admin: AdminDep, db: DbDep):
    """Admin: update a config. question_type cannot be changed here.

    When activating a config, any other active config for the same page_type is deactivated.
    """
    config = db.get(SurveyConfig, config_id)
    if not config:
        raise HTTPException(status_code=404, detail="Survey config not found")

    update_data = body.model_dump(exclude_unset=True)

    if update_data.get("is_active") is True and not config.is_active:
        other = db.scalar(
            select(SurveyConfig).where(
                SurveyConfig.page_type == config.page_type,
                SurveyConfig.is_active.is_(True),
                SurveyConfig.id != config_id,
            )
        )
        if other:
            other.is_active = False

    for field, value in update_data.items():
        setattr(config, field, value)

    db.commit()
    db.refresh(config)
    return config


@router.delete("/{config_id}", status_code=204)
def delete_config(config_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    """Admin: hard delete a survey config."""
    config = db.get(SurveyConfig, config_id)
    if not config:
        raise HTTPException(status_code=404, detail="Survey config not found")
    db.delete(config)
    db.commit()
