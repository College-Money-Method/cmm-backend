"""Phase 7 analytics: open/click aggregate math + SES event ingestion.

In-memory SQLite scoped to the two tables the analytics path touches
(``email_send_log`` + ``email_event``) — both use SQLite-compatible column
types, so no dialect shims are needed (unlike the scheduler conftest).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.db.base import Base
from src.emails import webhook_events
from src.emails.analytics import engagement_for_broadcast, engagement_for_source
from src.emails.models import EmailEvent, EmailSendLog

BROADCAST_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    tables = [t for n, t in Base.metadata.tables.items() if n in ("email_send_log", "email_event")]
    Base.metadata.create_all(engine, tables=tables)
    SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
    session = SessionLocal()
    yield session
    session.close()


def _sent(db, message_id: str, *, broadcast_id=BROADCAST_ID, source="broadcast", status="sent") -> EmailSendLog:
    log = EmailSendLog(
        recipient_email=f"{message_id}@example.com",
        subject="Hi",
        status=status,
        provider_message_id=message_id,
        source=source,
        broadcast_id=broadcast_id,
    )
    db.add(log)
    db.commit()
    return log


def _event(db, send_log_id, event_type, url=None):
    db.add(
        EmailEvent(
            send_log_id=send_log_id,
            event_type=event_type,
            url=url,
            occurred_at=datetime.now(timezone.utc),
        )
    )
    db.commit()


def test_open_and_click_rates_count_distinct_recipients(db):
    a = _sent(db, "m1")
    b = _sent(db, "m2")
    _sent(db, "m3")  # sent, never engaged
    # `a` opened 3x (MPP re-fetch) — must count as ONE unique open, not three.
    _event(db, a.id, "open")
    _event(db, a.id, "open")
    _event(db, a.id, "open")
    _event(db, b.id, "open")
    _event(db, a.id, "click", url="https://x")

    e = engagement_for_broadcast(db, BROADCAST_ID)
    assert e.sent_count == 3
    assert e.unique_opened == 2
    assert e.unique_clicked == 1
    assert e.open_rate == pytest.approx(2 / 3)
    assert e.click_rate == pytest.approx(1 / 3)


def test_rates_are_zero_when_nothing_sent(db):
    e = engagement_for_broadcast(db, BROADCAST_ID)
    assert e.sent_count == 0
    assert e.open_rate == 0.0
    assert e.click_rate == 0.0


def test_dry_run_rows_excluded_from_sent_count(db):
    live = _sent(db, "live")
    _sent(db, "dry", status="dry_run")
    _event(db, live.id, "open")
    e = engagement_for_broadcast(db, BROADCAST_ID)
    assert e.sent_count == 1
    assert e.unique_opened == 1


def test_engagement_for_source_filters_by_source(db):
    a = _sent(db, "pw1", broadcast_id=None, source="pre_workshop")
    _sent(db, "bc1", source="broadcast")
    _event(db, a.id, "open")
    e = engagement_for_source(db, "pre_workshop")
    assert e.sent_count == 1
    assert e.unique_opened == 1


def test_webhook_open_event_writes_linked_email_event(db, monkeypatch):
    log = _sent(db, "ses-msg-1")
    monkeypatch.setattr(webhook_events, "get_session_factory", lambda: sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    webhook_events.handle_open_event(
        {"eventType": "Open", "mail": {"messageId": "ses-msg-1"}, "open": {"timestamp": "2026-08-15T00:00:00Z"}}
    )
    events = db.query(EmailEvent).filter_by(send_log_id=log.id, event_type="open").all()
    assert len(events) == 1


def test_webhook_click_event_records_url(db, monkeypatch):
    log = _sent(db, "ses-msg-2")
    monkeypatch.setattr(webhook_events, "get_session_factory", lambda: sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    webhook_events.handle_click_event(
        {"eventType": "Click", "mail": {"messageId": "ses-msg-2"}, "click": {"link": "https://cmm.test/x", "timestamp": "2026-08-15T00:00:00Z"}}
    )
    event = db.query(EmailEvent).filter_by(send_log_id=log.id, event_type="click").one()
    assert event.url == "https://cmm.test/x"


def test_webhook_unresolvable_message_id_is_skipped_without_error(db, monkeypatch):
    monkeypatch.setattr(webhook_events, "get_session_factory", lambda: sessionmaker(bind=db.get_bind(), expire_on_commit=False))
    # No send log with this messageId — must not raise, must write nothing.
    webhook_events.handle_open_event({"eventType": "Open", "mail": {"messageId": "unknown"}, "open": {}})
    assert db.query(EmailEvent).count() == 0
