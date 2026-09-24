"""Global app config API router.

Exposes a single-row config: public GET for reading site-wide settings
(e.g. the welcome video) and an admin-only PATCH for editing them.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter
from sqlalchemy import select

from src.app_config.models import AppConfig
from src.app_config.operator_settings import reset_vimeo_folder_cache
from src.app_config.schemas import AppConfigOut, AppConfigUpdate
from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.schools.display_timezone import reset_app_default_timezone_cache

router = APIRouter(prefix="/api/v1/app-config", tags=["app-config"])


def _get_or_create(db) -> AppConfig:
    """Return the singleton config row, creating an empty one on first access."""
    cfg = db.scalar(select(AppConfig))
    if not cfg:
        cfg = AppConfig()
        db.add(cfg)
        db.commit()
        db.refresh(cfg)
    return cfg


@router.get("", response_model=AppConfigOut)
def get_app_config(db: DbDep):
    """Public: read the global app config — no auth required."""
    return _get_or_create(db)


@router.patch("", response_model=AppConfigOut)
def update_app_config(body: AppConfigUpdate, _admin: AdminDep, db: DbDep):
    """Admin: update the global app config."""
    cfg = _get_or_create(db)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(cfg, k, v)
    cfg.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(cfg)
    # Both of these are read behind a short in-process cache, on paths that
    # cannot open a session per call. Dropping them here means the admin who
    # just changed a value sees it take effect on the next run, not in five
    # minutes. Only this process's cache is cleared; another one catches up
    # when its own window expires.
    reset_app_default_timezone_cache()
    reset_vimeo_folder_cache()
    return cfg
