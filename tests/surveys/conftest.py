"""In-memory SQLite fixture for survey triage and digest tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.main  # noqa: F401 - imports every model module, registering them with Base.metadata
from src.db.base import Base
from src.surveys.models import SurveyResponse

_TABLES = ("survey_responses", "schools", "user_roles", "email_send_log", "email_suppression", "app_config")


@pytest.fixture
def survey_sessionmaker():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine, tables=[Base.metadata.tables[name] for name in _TABLES])
    return sessionmaker(bind=engine, expire_on_commit=False)


def add_response(session, **overrides) -> SurveyResponse:
    """Insert one response; defaults to a plain thumbs-up (not actionable)."""
    values = {
        "id": uuid.uuid4(),
        "created_at": datetime.now(timezone.utc),
        "page_type": "resource",
        "page_url": "https://example.com/school/acme/resources/fafsa",
        "resource_name": "FAFSA basics",
        "question_type": "thumbs",
        "question_text": "Was this helpful?",
        "rating_thumbs": True,
    }
    values.update(overrides)
    row = SurveyResponse(**values)
    session.add(row)
    session.commit()
    return row
