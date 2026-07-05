"""Durable DB-backed cache for PostHog query results.

Falls back to the in-process dict in posthog.py when no DB session is provided
(e.g. CLI scripts or background tasks outside request context).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, Session, mapped_column

from src.db.base import Base


class AnalyticsQueryCache(Base):
    __tablename__ = "analytics_query_cache"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ── Public helpers ─────────────────────────────────────────────────────────────

def db_cache_get(db: Session, key: str, ttl: timedelta) -> dict | None:
    """Return cached payload if present and within TTL, else None."""
    row = db.get(AnalyticsQueryCache, key)
    if row is None:
        return None
    age = datetime.now(timezone.utc) - row.fetched_at.replace(tzinfo=timezone.utc)
    if age <= ttl:
        return row.payload
    return None  # stale — caller decides whether to serve it


def db_cache_get_stale(db: Session, key: str) -> dict | None:
    """Return cached payload regardless of age (for stale-on-error serving)."""
    row = db.get(AnalyticsQueryCache, key)
    return row.payload if row else None


def db_cache_set(db: Session, key: str, payload: dict) -> None:
    """Upsert a cache entry."""
    row = db.get(AnalyticsQueryCache, key)
    if row is None:
        row = AnalyticsQueryCache(key=key, payload=payload)
        db.add(row)
    else:
        row.payload = payload
        row.fetched_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception:
        db.rollback()
