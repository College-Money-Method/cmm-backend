"""Weekday survey digest: who gets it, what is in it, and exactly-once delivery."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from src.auth.models import UserRole
from src.emails.models import EmailSendLog
from src.surveys import digest_job
from src.surveys.digest_email import DIGEST_ITEM_CAP, DigestStats, render_survey_digest
from src.surveys.digest_job import run_survey_digest
from src.surveys.models import SurveyResponse
from tests.surveys.conftest import add_response

ADMIN_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
ADMIN_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
DIRECTOR = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
EMAILS = {ADMIN_A: "a@example.com", ADMIN_B: "b@example.com", DIRECTOR: "director@example.com"}
# A Tuesday, 08:00 Pacific
TUESDAY_8AM = datetime(2026, 10, 13, 15, 0, tzinfo=timezone.utc)
MONDAY_8AM = datetime(2026, 10, 12, 15, 0, tzinfo=timezone.utc)


def resolver(user_ids):
    return [EMAILS[u] for u in user_ids]


@pytest.fixture
def session(survey_sessionmaker):
    s = survey_sessionmaker()
    s.add_all(
        [
            UserRole(user_id=ADMIN_A, role="super_admin"),
            UserRole(user_id=ADMIN_B, role="super_admin"),
            UserRole(user_id=DIRECTOR, role="hub_admin"),
        ]
    )
    s.commit()
    return s


def _logs(session) -> list[EmailSendLog]:
    return list(session.scalars(select(EmailSendLog).where(EmailSendLog.source == "survey_digest")))


def test_nothing_actionable_sends_nothing(session):
    add_response(session, rating_thumbs=True)
    add_response(session, rating_thumbs=None, question_type="stars", rating_stars=4)

    assert run_survey_digest(session, resolver, TUESDAY_8AM) == 0
    assert _logs(session) == []


def test_each_super_admin_gets_one_digest_and_rows_are_stamped(session):
    neg = add_response(session, rating_thumbs=False, comment="Video would not play")
    pos = add_response(session, rating_thumbs=True)

    assert run_survey_digest(session, resolver, TUESDAY_8AM) == 2

    logs = _logs(session)
    assert sorted(log.recipient_email for log in logs) == ["a@example.com", "b@example.com"]
    assert all("1 needs review" in log.subject for log in logs)
    session.expire_all()
    assert session.get(SurveyResponse, neg.id).digest_sent_at is not None
    assert session.get(SurveyResponse, pos.id).digest_sent_at is None  # not actionable, never claimed


def test_second_run_sends_nothing_and_new_rows_go_in_next_digest(session):
    add_response(session, rating_thumbs=False)
    run_survey_digest(session, resolver, TUESDAY_8AM)
    assert run_survey_digest(session, resolver, TUESDAY_8AM + timedelta(minutes=1)) == 0
    assert len(_logs(session)) == 2

    add_response(session, rating_thumbs=None, question_type="text", comment="Spanish version?")
    assert run_survey_digest(session, resolver, TUESDAY_8AM + timedelta(days=1)) == 2
    assert len(_logs(session)) == 4


def test_already_reviewed_response_is_not_emailed(session):
    row = add_response(session, rating_thumbs=False, reviewed_at=TUESDAY_8AM - timedelta(minutes=30))

    assert run_survey_digest(session, resolver, TUESDAY_8AM) == 0
    session.expire_all()
    assert session.get(SurveyResponse, row.id).digest_sent_at is None


def test_no_resolvable_admin_releases_the_claim(session):
    row = add_response(session, rating_thumbs=False)

    assert run_survey_digest(session, lambda ids: [], TUESDAY_8AM) == 0
    session.expire_all()
    assert session.get(SurveyResponse, row.id).digest_sent_at is None


def test_all_sends_failing_releases_the_claim(session, monkeypatch):
    row = add_response(session, rating_thumbs=False)

    def boom(*args, **kwargs):
        raise RuntimeError("SES down")

    monkeypatch.setattr(digest_job, "send_email", boom)
    assert run_survey_digest(session, resolver, TUESDAY_8AM) == 0
    session.expire_all()
    assert session.get(SurveyResponse, row.id).digest_sent_at is None


def test_job_registered_for_weekday_mornings_pacific():
    class FakeScheduler:
        def add_job(self, func, trigger, **kwargs):
            self.call = (func, trigger, kwargs)

    sched = FakeScheduler()
    digest_job.register_survey_digest_job(sched)
    func, trigger, kwargs = sched.call
    assert func is run_survey_digest and trigger == "cron"
    assert kwargs["day_of_week"] == "mon-fri" and kwargs["hour"] == 8
    assert str(kwargs["timezone"]) == "America/Los_Angeles"


def test_monday_stats_cover_the_weekend(session):
    add_response(session, rating_thumbs=True, created_at=MONDAY_8AM - timedelta(days=2))
    stats = digest_job._stats(session, MONDAY_8AM)
    assert stats.period_label == "since Friday" and stats.total == 1
    assert digest_job._stats(session, TUESDAY_8AM).total == 0


# ── rendering ────────────────────────────────────────────────────────────────

STATS = DigestStats(period_label="in the last 24 hours", total=40, thumbs_up=30, thumbs_down=6, avg_stars=4.2)


def _row(**kw) -> SurveyResponse:
    values = {
        "id": uuid.uuid4(),
        "created_at": TUESDAY_8AM - timedelta(hours=2),
        "page_type": "workshop",
        "page_url": "/school/acme/workshops/fafsa-101",
        "resource_name": "FAFSA 101",
        "school_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "question_type": "thumbs",
        "question_text": "Helpful?",
        "rating_thumbs": False,
    }
    values.update(kw)
    return SurveyResponse(**values)


def test_render_escapes_user_text_and_drops_unsafe_links():
    row = _row(comment="<script>alert(1)</script>", resource_name="<b>x</b>", page_url="javascript:alert(1)")
    subject, html, text = render_survey_digest([row], STATS, {}, "https://app.example.com", TUESDAY_8AM)

    assert subject == "Survey feedback: 1 needs review (Oct 13)"
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>x</b>" not in html
    assert "javascript:" not in html
    _, foreign, _ = render_survey_digest(
        [_row(page_url="https://evil.example/school/acme?x=1")], STATS, {}, "https://app.example.com", TUESDAY_8AM
    )
    assert "evil.example" not in foreign
    assert "https://app.example.com/school/acme?x=1" in foreign
    assert "https://app.example.com/admin/surveys/responses?status=needs_review" in html
    assert "<script>alert(1)</script>" in text  # plain text is not HTML


def test_render_makes_relative_page_urls_absolute_and_names_the_school():
    _, html, _ = render_survey_digest(
        [_row()], STATS, {"aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa": "Acme High"}, "https://app.example.com", TUESDAY_8AM
    )
    assert "https://app.example.com/school/acme/workshops/fafsa-101" in html
    assert "Workshop · FAFSA 101 · Acme High" in html
    assert "40 responses in the last 24 hours · 30 👍 / 6 👎 · avg 4.2★" in html


def test_render_caps_the_list():
    rows = [_row(created_at=TUESDAY_8AM - timedelta(minutes=i)) for i in range(DIGEST_ITEM_CAP + 7)]
    subject, html, text = render_survey_digest(rows, STATS, {}, "https://app.example.com", TUESDAY_8AM)

    assert "32 need review" in subject
    assert html.count("View page") == DIGEST_ITEM_CAP
    assert "+7 more" in html and "+7 more" in text


def test_one_failed_recipient_does_not_unclaim(session, monkeypatch):
    row = add_response(session, rating_thumbs=False)
    real_send = digest_job.send_email

    def flaky(db, *, to, **kwargs):
        if to == "a@example.com":
            raise RuntimeError("bounced")
        return real_send(db, to=to, **kwargs)

    monkeypatch.setattr(digest_job, "send_email", flaky)
    assert run_survey_digest(session, resolver, TUESDAY_8AM) == 1
    session.expire_all()
    assert session.get(SurveyResponse, row.id).digest_sent_at is not None
