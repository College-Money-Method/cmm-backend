"""SQLAlchemy engine, session factory, and declarative Base."""

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import settings


class Base(DeclarativeBase):
    pass


@lru_cache(maxsize=None)
def get_engine(url: str | None = None):
    # Cached: one engine (and one connection pool) per worker process. Without this
    # the engine was rebuilt on every request, so each request got its own pool and
    # the sizing below was meaningless. Now it is the real per-process concurrency
    # ceiling — pages that fan out parallel API calls (e.g. the analytics dashboard)
    # need enough connections to serve the burst, or requests queue and hit
    # pool_timeout with "QueuePool limit ... reached".
    #
    # Prod connects via Supabase transaction pooler (port 6543), which multiplexes
    # many client connections onto few Postgres backends and returns the backend
    # after each transaction. psycopg2 issues no server-side prepared statements, so
    # transaction mode needs no extra config. Do NOT use the session pooler (5432):
    # it pins one backend per connection for its whole life and exhausts the limit.
    #
    # Budget: per process = pool_size + max_overflow = 20 client conns. 3 workers x
    # 3 tasks = 9 processes -> up to 180 connections to Supavisor, which multiplexes
    # them onto far fewer Postgres backends (Pro max_connections = 90 is not the
    # binding limit for pooler client conns). pool_recycle avoids stale-connection
    # errors when Supavisor drops idle backends; pool_timeout fails fast (10s)
    # instead of hanging 30s when the pool is momentarily saturated.
    return create_engine(
        url or settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=10,
        pool_timeout=10,
        pool_recycle=1800,
    )


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)
