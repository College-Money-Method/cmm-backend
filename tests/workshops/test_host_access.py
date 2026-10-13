"""Host-page token minting, rotation and session end."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, update

from src.workshops.host_access import (
    effective_end,
    ensure_host_access_token,
    host_questions_url,
    rotate_host_access_token,
)
from src.workshops.models import Webinar, Workshop

START = datetime(2026, 5, 1, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def db(webinar_sessionmaker):
    session = webinar_sessionmaker()
    session.add(Workshop(id=uuid.UUID(int=1), name="FAFSA"))
    session.commit()
    yield session
    session.close()


def _webinar(db, **kw) -> Webinar:
    w = Webinar(id=uuid.uuid4(), workshop_id=uuid.UUID(int=1), **kw)
    db.add(w)
    db.commit()
    return w


def test_token_is_minted_once_and_stable(db):
    w = _webinar(db, start_datetime=START)
    first = ensure_host_access_token(db, w)
    assert len(first) == 43
    assert ensure_host_access_token(db, w) == first
    assert w.host_access_token == first


def test_concurrent_mint_does_not_overwrite(db, webinar_sessionmaker):
    """Another worker sets the token between our read and our write."""
    w = _webinar(db, start_datetime=START)
    other = webinar_sessionmaker()
    fired = []

    @event.listens_for(db, "do_orm_execute")
    def _rival_wins_first(state):
        if state.is_update and not fired:
            fired.append(1)
            other.execute(update(Webinar).where(Webinar.id == w.id).values(host_access_token="r" * 43))
            other.commit()

    assert ensure_host_access_token(db, w) == "r" * 43
    assert fired
    other.close()


def test_rotate_changes_token(db):
    w = _webinar(db, start_datetime=START)
    first = ensure_host_access_token(db, w)
    second = rotate_host_access_token(db, w)
    assert second != first
    assert w.host_access_token == second


def test_effective_end(db):
    end = START + timedelta(hours=1)
    normal = _webinar(db, start_datetime=START, end_datetime=end)
    assert effective_end(normal) == end

    no_end = _webinar(db, start_datetime=START)
    assert effective_end(no_end) == START + timedelta(hours=2)

    no_start = _webinar(db)
    assert effective_end(no_start) is None


def test_url_uses_email_origin(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "app_public_url", "https://app.example.org/")
    assert host_questions_url("tok") == "https://app.example.org/workshop-hosts/tok"
