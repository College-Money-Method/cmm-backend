"""Durable DB-backed cache for PostHog query results.

Falls back to the in-process dict in posthog.py when no DB session is provided
(e.g. CLI scripts or background tasks outside request context).
"""

from __future__ import annotations

import hashlib
import logging
import random
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator

from sqlalchemy import Text, text
from sqlalchemy.dialects.postgresql import JSONB, TIMESTAMP
from sqlalchemy.orm import Mapped, Session, mapped_column

from src.db.base import Base

logger = logging.getLogger(__name__)


class AnalyticsQueryCache(Base):
    __tablename__ = "analytics_query_cache"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ── Served-cache-age tracking ──────────────────────────────────────────────────
#
# Endpoints report `cached_at` — when the numbers they return were ACTUALLY read
# from PostHog — so the UI can label real data age and gate its Refresh button on
# it. Every cache hit records the entry's `fetched_at` on the request-scoped
# Session (`db.info`), and the endpoint reads the OLDEST one back at return time.
#
# Why `db.info` and not a ContextVar: these endpoints are sync (`def`), so FastAPI
# runs them in a threadpool with a COPIED context — a ContextVar set inside would
# never be visible to the caller. The Session is one object per request, shared by
# every cache helper below, which makes it the natural carrier.
_CACHE_HITS_KEY = "analytics_cache_hits"


def _note_hit(db: Session, fetched_at: datetime) -> None:
    """Record that a cache entry written at `fetched_at` was served this request."""
    db.info.setdefault(_CACHE_HITS_KEY, []).append(fetched_at)


def oldest_cache_hit(db: Session | None) -> datetime | None:
    """Oldest cache entry served during this request — the true age of the
    response. None when nothing came from cache: everything was just computed.

    Tolerates a non-Session `db` (tests pass mocks, CLI callers pass None) by
    reporting None rather than raising — a missing timestamp degrades to "just
    now", which is the same thing a fresh compute reports."""
    info = getattr(db, "info", None)
    hits = info.get(_CACHE_HITS_KEY) if isinstance(info, dict) else None
    if not isinstance(hits, list) or not hits:
        return None
    return min(hits)


# ── Public helpers ─────────────────────────────────────────────────────────────

def db_cache_get(db: Session, key: str, ttl: timedelta) -> dict | None:
    """Return cached payload if present and within TTL, else None."""
    # populate_existing: single_flight's post-wait re-check reads the same key this
    # request already read (and found expired) before waiting. Without it, get()
    # would hand back the instance still in the identity map — the pre-wait,
    # pre-refresh row — so the waiter would recompute what it just waited for.
    row = db.get(AnalyticsQueryCache, key, populate_existing=True)
    if row is None:
        return None
    fetched_at = row.fetched_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - fetched_at <= ttl:
        _note_hit(db, fetched_at)
        return row.payload
    return None  # stale — caller decides whether to serve it


def db_cache_get_stale(db: Session, key: str) -> dict | None:
    """Return cached payload regardless of age (for stale-on-error serving)."""
    row = db.get(AnalyticsQueryCache, key, populate_existing=True)
    if row is None:
        return None
    # Counts as a hit — and an old one, which is exactly what the UI should show
    # (a PostHog outage is when "this data is 3 hours old" matters most).
    _note_hit(db, row.fetched_at.replace(tzinfo=timezone.utc))
    return row.payload


def db_cache_set(db: Session, key: str, payload: dict) -> None:
    """Upsert a cache entry, then occasionally purge long-abandoned rows."""
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
    # Opportunistic TTL cleanup: on a small fraction of writes, sweep out rows no
    # request has refreshed in a long time so the table can't grow without bound.
    # Actively-used keys are rewritten every TTL (30–60 min) and never age out;
    # only keys nobody queries anymore reach _MAX_AGE. Runs on writes (not a
    # scheduler/cron) to stay infra-free — write volume is what fills the table,
    # so it's also what should drain it. Best-effort: never fails a cache write.
    if random.random() < _CLEANUP_PROBABILITY:
        _purge_stale(db)


# How long an untouched entry lives before opportunistic cleanup removes it. Well
# beyond every TTL, so this only reaps abandoned keys — and generous enough that
# stale-on-error serving (db_cache_get_stale, any age) still has a fallback for
# recently-active keys during a PostHog outage.
_MAX_AGE = timedelta(days=7)

# Fraction of writes that trigger a purge sweep. Low so the extra DELETE is
# negligible per request, but with regular analytics traffic the table is still
# swept many times a day.
_CLEANUP_PROBABILITY = 0.02


def _purge_stale(db: Session) -> None:
    """Delete cache rows older than _MAX_AGE. Best-effort; swallows errors so a
    failed sweep never surfaces to the caller that just wrote its entry."""
    cutoff = datetime.now(timezone.utc) - _MAX_AGE
    try:
        result = db.execute(
            text("DELETE FROM analytics_query_cache WHERE fetched_at < :cutoff"),
            {"cutoff": cutoff},
        )
        db.commit()
        if result.rowcount:
            logger.info("analytics cache: purged %d stale rows", result.rowcount)
    except Exception:
        db.rollback()
        logger.warning("analytics cache: stale-row purge failed", exc_info=True)


# ── Cross-process single-flight (request coalescing) ────────────────────────

def _advisory_key(key: str) -> int:
    """Derive a signed 64-bit int from a cache key for pg_advisory_lock.

    Postgres advisory lock functions take a `bigint` (signed 64-bit), so the
    first 8 bytes of the key's MD5 digest are reinterpreted as a signed int
    (big-endian) rather than truncated/masked to unsigned — an unsigned value
    above 2^63-1 would overflow the bigint column/parameter.
    """
    return int.from_bytes(hashlib.md5(key.encode()).digest()[:8], "big", signed=True)


# How long to wait for another request's compute before giving up and computing
# anyway. A duplicate PostHog round trip is far cheaper than a hung page: the
# frontend streams these payloads and aborts at 30s (entry.server streamTimeout).
_LOCK_WAIT = "10s"


@contextmanager
def single_flight(db: Session | None, key: str) -> Iterator[None]:
    """Serialize concurrent computations of the same cache key across processes.

    Uses a TRANSACTION-scoped advisory lock (pg_advisory_xact_lock). Postgres
    releases it at transaction end no matter what — commit, rollback, error, or a
    dropped connection — so a lock can never be orphaned.

    Releasing at commit is exactly the handoff we want, not a limitation: the
    commit that ends the transaction is db_cache_set()'s, i.e. the moment the
    cache entry is published. A waiter woken there re-reads and finds the fresh
    row (READ COMMITTED shows it the latest committed data).

    It must NOT be the session-level pg_advisory_lock: that lock belongs to the
    CONNECTION, and db_cache_set()'s mid-computation db.commit() returns the
    connection to the pool. The matching pg_advisory_unlock then ran on whatever
    connection the pool handed back — a different one under concurrency (measured:
    7 of 8 times) — so it silently returned false and left the lock held by an
    idle pooled connection FOREVER. Every later request for that key blocked on
    it, which surfaced as analytics tabs hanging until the 30s stream timeout.
    Same reason it can't work through Supabase's transaction pooler in prod,
    where consecutive transactions need not share a server connection at all.

    If db is None (CLI / background jobs, no request-scoped session), this is a
    no-op — those callers already fall back to the in-process dict cache in
    posthog.py, which needs no cross-process coordination.
    """
    if db is None:
        yield
        return
    k = _advisory_key(key)
    try:
        # Bounded wait: lock_timeout covers advisory-lock acquisition, so a
        # pathological holder degrades us to duplicate work, never to a hang.
        # LOCAL = for this transaction only, undone by the next commit.
        db.execute(text(f"SET LOCAL lock_timeout = '{_LOCK_WAIT}'"))
        db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": k})
    except Exception:
        # Timed out (or the lock statement failed) — the transaction is aborted,
        # so roll back before the caller runs its own queries, then compute
        # WITHOUT coalescing. Correctness never depended on the lock.
        logger.warning("single_flight lock unavailable for %s; computing uncoalesced", key)
        db.rollback()
    yield
    # No unlock: the lock ends with the transaction. Whatever the caller did —
    # committed the fresh entry, raised, or returned early — Postgres cleans up.
