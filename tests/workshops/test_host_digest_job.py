"""Host digest job: selection, recipients, Zoom fallback, per-recipient claims."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from src.config import settings
from src.emails.models import EmailSendLog
from src.integrations.zoom_webinar_hosts import ZoomWebinarNotFound
from src.workshops import host_digest_job
from src.workshops.host_digest_job import register_host_digest_job, run_host_digest
from src.workshops.models import Webinar, WebinarHostDigestDelivery, Workshop

PAUL = "paul.martin@collegemoneymethod.com"
NOW = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)  # Tue 08:00 Pacific
TOMORROW_START = datetime(2026, 10, 14, 22, 0, tzinfo=timezone.utc)  # ~31h out


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "webinar_host_digest_enabled", True)
    monkeypatch.setattr(settings, "webinar_host_digest_extra_recipients", [PAUL])


@pytest.fixture
def db(webinar_sessionmaker):
    s = webinar_sessionmaker()
    s.add(Workshop(id=uuid.UUID(int=1), name="FAFSA"))
    s.commit()
    yield s
    s.close()


@pytest.fixture
def zoom(monkeypatch):
    """Controllable co-host lookup: set ``zoom.value`` to a list, or None for failure."""
    state = SimpleNamespace(value=["host@x.com"], calls=0)

    def fake(_zoom_id):
        state.calls += 1
        return state.value

    monkeypatch.setattr(host_digest_job, "get_webinar_cohost_emails", fake)
    return state


@pytest.fixture
def mail(monkeypatch):
    """Records sends; ``mail.status_for`` maps recipient -> status (default "sent")."""
    state = SimpleNamespace(sent=[], status_for={})

    def fake_send(db, to, subject, html, text, source, **kw):
        state.sent.append(SimpleNamespace(to=to, subject=subject, text=text, source=source, **kw))
        return SimpleNamespace(status=state.status_for.get(to, "sent"))

    monkeypatch.setattr(host_digest_job, "send_email", fake_send)
    return state


def _webinar(db, start=TOMORROW_START, **kw) -> Webinar:
    kw.setdefault("zoom_webinar_id", str(uuid.uuid4().int)[:11])
    w = Webinar(id=uuid.uuid4(), workshop_id=uuid.UUID(int=1), webinar_name="FAFSA Night", start_datetime=start, **kw)
    db.add(w)
    db.commit()
    return w


def _recipients(mail) -> list[str]:
    return [m.to for m in mail.sent]


def test_selection_window(db, zoom, mail):
    due = _webinar(db)
    _webinar(db, start=NOW + timedelta(hours=3))  # later today: also due
    _webinar(db, start=NOW - timedelta(minutes=5))  # already started
    _webinar(db, start=datetime(2026, 10, 15, 8, 0, tzinfo=timezone.utc))  # day after tomorrow (LA)
    _webinar(db, start=None)
    _webinar(db, zoom_webinar_id=None)

    due_ids = {w.id for w in host_digest_job._select_due(db, NOW)}
    assert due.id in due_ids and len(due_ids) == 2


def test_sends_cohosts_then_paul_with_webinar_id_and_source(db, zoom, mail):
    w = _webinar(db)
    zoom.value = ["a@x.com", "B@x.com"]

    assert run_host_digest(db, NOW) == 3
    assert _recipients(mail) == ["a@x.com", "b@x.com", PAUL]
    assert all(m.source == "webinar_host_digest" and m.webinar_id == w.id for m in mail.sent)
    assert mail.sent[0].subject.startswith("Registrant questions: FAFSA · ")  # workshop title, not session name
    assert "/workshop-hosts/" in mail.sent[0].text


def test_dedupe_case_insensitive_and_paul_always_included(db, zoom, mail):
    _webinar(db)
    zoom.value = [PAUL.upper(), "a@x.com"]
    run_host_digest(db, NOW)
    assert sorted(_recipients(mail)) == sorted([PAUL, "a@x.com"])

    zoom.value = []  # no co-hosts: Paul still gets it
    mail.sent.clear()
    db.execute(WebinarHostDigestDelivery.__table__.delete())
    db.commit()
    run_host_digest(db, NOW)
    assert _recipients(mail) == [PAUL]


def test_zoom_failure_far_from_start_defers_without_rows(db, zoom, mail):
    _webinar(db)
    zoom.value = None
    assert run_host_digest(db, NOW) == 0
    assert mail.sent == []
    assert db.scalars(select(WebinarHostDigestDelivery)).all() == []


def test_zoom_failure_close_to_start_sends_paul_with_banner_then_cohosts_on_recovery(db, zoom, mail):
    _webinar(db, start=NOW + timedelta(hours=3))
    zoom.value = None
    assert run_host_digest(db, NOW) == 1
    assert _recipients(mail) == [PAUL]
    assert "forward this link" in mail.sent[0].text

    zoom.value = ["a@x.com"]
    mail.sent.clear()
    assert run_host_digest(db, NOW + timedelta(hours=1)) == 1
    assert _recipients(mail) == ["a@x.com"]  # Paul already handled


def test_webinar_deleted_in_zoom_is_skipped_even_close_to_start(db, zoom, mail, monkeypatch):
    _webinar(db, start=NOW + timedelta(hours=3))

    def gone(_zoom_id):
        raise ZoomWebinarNotFound(_zoom_id)

    monkeypatch.setattr(host_digest_job, "get_webinar_cohost_emails", gone)
    assert run_host_digest(db, NOW) == 0
    assert mail.sent == []
    assert db.scalars(select(WebinarHostDigestDelivery)).all() == []


def test_claim_abandoned_mid_send_is_taken_over_once_stale(db, zoom, mail):
    w = _webinar(db)
    zoom.value = []
    # Simulate a worker killed between claim and send: row stuck at "claimed".
    db.add(WebinarHostDigestDelivery(
        webinar_id=w.id, recipient_email=PAUL, start_datetime=w.start_datetime, status="claimed",
        updated_at=datetime.now(timezone.utc),
    ))
    db.commit()
    assert run_host_digest(db, NOW) == 0  # fresh claim: another worker may still be sending

    row = db.scalars(select(WebinarHostDigestDelivery)).one()
    row.updated_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    assert run_host_digest(db, NOW) == 1
    assert _recipients(mail) == [PAUL]
    db.refresh(row)
    assert row.status == "sent"


def test_send_exception_releases_row_and_next_tick_retries(db, zoom, mail, monkeypatch):
    _webinar(db)
    zoom.value = []
    real = host_digest_job.send_email

    def boom(*a, **kw):
        raise RuntimeError("ses down")

    monkeypatch.setattr(host_digest_job, "send_email", boom)
    assert run_host_digest(db, NOW) == 0
    assert db.scalars(select(WebinarHostDigestDelivery)).all() == []
    monkeypatch.setattr(host_digest_job, "send_email", real)
    assert run_host_digest(db, NOW + timedelta(hours=1)) == 1


def test_missing_public_origin_skips_without_claims(db, zoom, mail, monkeypatch):
    from src.workshops import host_access

    _webinar(db)
    monkeypatch.setattr(host_access, "email_origin", lambda: "")
    assert run_host_digest(db, NOW) == 0
    assert mail.sent == []
    assert db.scalars(select(WebinarHostDigestDelivery)).all() == []


def test_failed_send_releases_row_and_next_tick_retries_only_them(db, zoom, mail):
    _webinar(db)
    zoom.value = ["a@x.com"]
    mail.status_for["a@x.com"] = "failed"

    assert run_host_digest(db, NOW) == 1  # only Paul
    assert "Not delivered to: a@x.com (failed)" in mail.sent[-1].text
    rows = db.scalars(select(WebinarHostDigestDelivery)).all()
    assert [r.recipient_email for r in rows] == [PAUL]

    mail.sent.clear()
    mail.status_for.clear()
    assert run_host_digest(db, NOW + timedelta(hours=1)) == 1
    assert _recipients(mail) == ["a@x.com"]


def test_suppressed_row_is_kept_and_listed_for_paul(db, zoom, mail):
    _webinar(db)
    zoom.value = ["a@x.com"]
    mail.status_for["a@x.com"] = "suppressed"

    run_host_digest(db, NOW)
    assert "Not delivered to: a@x.com (suppressed)" in mail.sent[-1].text
    statuses = {r.recipient_email: r.status for r in db.scalars(select(WebinarHostDigestDelivery))}
    assert statuses == {"a@x.com": "suppressed", PAUL: "sent"}

    mail.sent.clear()
    assert run_host_digest(db, NOW + timedelta(hours=1)) == 0  # not retried
    assert mail.sent == []


def test_reschedule_sends_everyone_again(db, zoom, mail):
    w = _webinar(db)
    zoom.value = ["a@x.com"]
    run_host_digest(db, NOW)
    mail.sent.clear()

    w.start_datetime = TOMORROW_START + timedelta(hours=1)
    db.commit()
    assert run_host_digest(db, NOW + timedelta(hours=1)) == 2
    assert _recipients(mail) == ["a@x.com", PAUL]


def test_second_tick_sends_nothing(db, zoom, mail):
    _webinar(db)
    assert run_host_digest(db, NOW) == 2
    assert run_host_digest(db, NOW + timedelta(hours=1)) == 0
    assert len(mail.sent) == 2


def test_flag_off_sends_nothing(db, zoom, mail, monkeypatch):
    _webinar(db)
    monkeypatch.setattr(settings, "webinar_host_digest_enabled", False)
    assert run_host_digest(db, NOW) == 0
    assert mail.sent == [] and zoom.calls == 0


def test_one_failing_webinar_does_not_stop_the_batch(db, zoom, mail, monkeypatch):
    bad = _webinar(db, start=TOMORROW_START)
    good = _webinar(db, start=TOMORROW_START + timedelta(hours=1))

    def fake(zoom_id):
        if zoom_id == bad.zoom_webinar_id:
            raise RuntimeError("boom")
        return ["a@x.com"]

    monkeypatch.setattr(host_digest_job, "get_webinar_cohost_emails", fake)
    assert run_host_digest(db, NOW) == 2
    assert {m.webinar_id for m in mail.sent} == {good.id}


def test_real_send_logs_webinar_id(db, zoom):
    w = _webinar(db)
    zoom.value = ["a@x.com"]
    run_host_digest(db, NOW)
    logs = db.scalars(select(EmailSendLog).where(EmailSendLog.source == "webinar_host_digest")).all()
    assert {log.recipient_email for log in logs} == {"a@x.com", PAUL}
    assert all(log.webinar_id == w.id for log in logs)


def test_non_leader_skips_sessionless_run(monkeypatch, caplog):
    monkeypatch.setattr(host_digest_job.single_writer, "is_leader", lambda _name: False)
    with caplog.at_level("INFO"):
        assert run_host_digest(now=NOW) == 0
    assert "not the scheduler leader" in caplog.text


def test_registers_hourly_cron_job():
    calls = []
    scheduler = SimpleNamespace(add_job=lambda *a, **kw: calls.append((a, kw)))
    register_host_digest_job(scheduler)
    (args, kw), = calls
    assert args == (run_host_digest, "cron")
    assert kw["id"] == "webinar_host_digest_hourly"
    assert (kw["hour"], kw["minute"]) == ("8-20", 0)
    assert str(kw["timezone"]) == "America/Los_Angeles"
    assert kw["replace_existing"] and kw["coalesce"] and kw["max_instances"] == 1
