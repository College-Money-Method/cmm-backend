"""Durable DB-backed cache for PostHog query results.

Falls back to the in-process dict in posthog.py when no DB session is provided
(e.g. CLI scripts or background tasks outside request context).
"""

from __future__ import annotations

import hashlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import Text, text
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


# ── Cross-process single-flight (request coalescing) ────────────────────────

def _advisory_key(key: str) -> int:
    """Derive a signed 64-bit int from a cache key for pg_advisory_lock.

    Postgres advisory lock functions take a `bigint` (signed 64-bit), so the
    first 8 bytes of the key's MD5 digest are reinterpreted as a signed int
    (big-endian) rather than truncated/masked to unsigned — an unsigned value
    above 2^63-1 would overflow the bigint column/parameter.
    """
    return int.from_bytes(hashlib.md5(key.encode()).digest()[:8], "big", signed=True)


@contextmanager
def single_flight(db: Session | None, key: str) -> Iterator[None]:
    """Serialize concurrent computations of the same cache key across processes.

    Uses a SESSION-level Postgres advisory lock (pg_advisory_lock /
    pg_advisory_unlock) — NOT the transaction-scoped pg_advisory_xact_lock.
    Compute sites call db_cache_set() mid-computation, which does db.commit();
    a transaction-scoped lock would be released at that commit, defeating the
    purpose. A session-level lock instead stays held across commits until
    explicitly released here (in `finally`), so a second request for the same
    key genuinely waits for the first request's full compute-and-write to finish.

    If db is None (CLI / background jobs, no request-scoped session), this is a
    no-op — those callers already fall back to the in-process dict cache in
    posthog.py, which needs no cross-process coordination.
    """
    if db is None:
        yield
        return
    k = _advisory_key(key)
    db.execute(text("SELECT pg_advisory_lock(:k)"), {"k": k})
    try:
        yield
    finally:
        db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": k})
