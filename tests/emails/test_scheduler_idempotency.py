"""Idempotency test for the automation runner: running the check twice must
not send a second batch of emails for a mapping already recorded in
`automation_send_ledger` on the first run. Uses the shared in-memory SQLite
fixture from `tests/emails/conftest.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.emails import automation_runner
from src.emails.automation_ledger_models import AutomationSendLedger
from src.emails.automation_models import EmailAutomation
from src.emails.automation_runner import run_automations_check
from src.emails.email_template_models import EmailTemplate
from src.emails.models import EmailSendLog
from src.schools.models import Contact, School
from src.workshops.models import PortalMapping, Webinar, Workshop

SCHOOL_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
FAMILY_CONTACT_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
WORKSHOP_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
WEBINAR_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
TEMPLATE_ID = uuid.UUID("33333333-2222-2222-2222-222222222222")
AUTOMATION_ID = uuid.UUID("44444444-2222-2222-2222-222222222222")


@pytest.fixture
def db_session(scheduler_sessionmaker):
    session = scheduler_sessionmaker()
    session.add(School(id=SCHOOL_ID, name="Test High", slug="test-high", is_current_customer=True))
    session.add(
        Contact(
            id=FAMILY_CONTACT_ID,
            school_id=SCHOOL_ID,
            email="family@example.com",
            role="hub_user",
            auto_emails=True,
        )
    )
    session.add(Workshop(id=WORKSHOP_ID, name="FAFSA Basics"))
    session.add(
        Webinar(
            id=WEBINAR_ID,
            workshop_id=WORKSHOP_ID,
            # offset_value=7 days "before" -> fire_at = start - 7d; a start 6
            # days out puts fire_at 1 day in the past, inside the 2-day
            # catch-up window (due window on `start` itself is [now+5d, now+7d]).
            start_datetime=datetime.now(timezone.utc) + timedelta(days=6),
            registration_url="https://zoom.example.com/register",
        )
    )
    session.add(
        EmailTemplate(
            id=TEMPLATE_ID,
            category="workshop",
            name="Global announcement",
            subject="Reminder: {{workshop_name}}",
            body_json='{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"See you at {{workshop_name}} on {{date}}."}]}]}',
        )
    )
    session.add(PortalMapping(id=uuid.uuid4(), school_id=SCHOOL_ID, webinar_id=WEBINAR_ID))
    session.add(
        EmailAutomation(
            id=AUTOMATION_ID,
            name="Pre-Workshop Reminder",
            type="pre_workshop_reminder",
            enabled=True,
            offset_value=7,
            offset_unit="days",
            offset_direction="before",
            template_id=TEMPLATE_ID,
        )
    )
    session.commit()

    yield session
    session.close()


def test_second_run_sends_zero_emails_for_already_ledgered_mapping(db_session):
    first_sent = run_automations_check(db_session)
    assert first_sent == 1

    logs_after_first = db_session.query(EmailSendLog).filter(EmailSendLog.source == "pre_workshop").all()
    assert len(logs_after_first) == 1
    assert logs_after_first[0].recipient_email == "family@example.com"
    assert logs_after_first[0].automation_id == AUTOMATION_ID

    mapping = db_session.query(PortalMapping).filter(PortalMapping.webinar_id == WEBINAR_ID).one()
    ledger_row = (
        db_session.query(AutomationSendLedger)
        .filter(
            AutomationSendLedger.automation_id == AUTOMATION_ID,
            AutomationSendLedger.portal_mapping_id == mapping.id,
        )
        .one_or_none()
    )
    assert ledger_row is not None

    second_sent = run_automations_check(db_session)
    assert second_sent == 0

    logs_after_second = db_session.query(EmailSendLog).filter(EmailSendLog.source == "pre_workshop").all()
    assert len(logs_after_second) == 1  # unchanged — no duplicate send


def test_batch_where_every_send_fails_releases_claim_and_retries(db_session, monkeypatch):
    """A provider outage must not burn the mapping: the claim is released so a
    later run redelivers, and that retry is still exactly-once."""
    def _boom(*args, **kwargs):
        raise RuntimeError("SES unavailable")

    monkeypatch.setattr("src.emails.automation_runner.send_email", _boom)
    assert run_automations_check(db_session) == 0

    mapping = db_session.query(PortalMapping).filter(PortalMapping.webinar_id == WEBINAR_ID).one()
    assert (
        db_session.query(AutomationSendLedger)
        .filter(
            AutomationSendLedger.automation_id == AUTOMATION_ID,
            AutomationSendLedger.portal_mapping_id == mapping.id,
        )
        .one_or_none()
        is None
    )

    monkeypatch.undo()
    assert run_automations_check(db_session) == 1
    assert len(db_session.query(EmailSendLog).filter(EmailSendLog.source == "pre_workshop").all()) == 1

    assert run_automations_check(db_session) == 0  # claim now held — no duplicate


def test_claim_is_committed_before_the_first_send(db_session):
    """Guards the concurrency fix: the ledger row must already be visible when
    the first email goes out, because `send_email` commits its own log row and
    would otherwise release any row lock held over the batch."""
    mapping = db_session.query(PortalMapping).filter(PortalMapping.webinar_id == WEBINAR_ID).one()
    seen_claim: list[bool] = []

    real_send = automation_runner.send_email

    def _spy(db, *args, **kwargs):
        seen_claim.append(
            db.query(AutomationSendLedger)
            .filter(
                AutomationSendLedger.automation_id == AUTOMATION_ID,
                AutomationSendLedger.portal_mapping_id == mapping.id,
            )
            .one_or_none()
            is not None
        )
        return real_send(db, *args, **kwargs)

    automation_runner.send_email = _spy
    try:
        assert run_automations_check(db_session) == 1
    finally:
        automation_runner.send_email = real_send

    assert seen_claim == [True]
