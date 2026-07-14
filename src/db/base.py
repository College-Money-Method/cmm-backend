"""SQLAlchemy engine, session factory, and declarative Base."""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from src.config import settings


class Base(DeclarativeBase):
    pass


def get_engine(url: str | None = None):
    # Sized for Supabase free tier + session pooler (port 5432): each app connection
    # holds one dedicated Supavisor->Postgres backend. Budget is the pooler pool size
    # (~15 on free), shared across all Fargate tasks + migrations + scripts.
    # Max per task = pool_size + max_overflow = 3; x3 tasks = 9, leaving headroom.
    # pool_recycle avoids stale-connection errors when Supavisor drops idle backends.
    return create_engine(
        url or settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=1,
        pool_recycle=1800,
    )


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False)
