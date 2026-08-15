"""Guard-rail tests for the automation runner:

1. `EmailAutomation.enabled=False` must skip every send, even when a workshop
   is due within the offset window (automations ship dark by default).
2. The `Contact.auto_emails` opt-in filter is mandatory and has no override —
   a contact with `auto_emails=False` must never receive an automation send,
   even when the automation is enabled.

Uses the shared in-memory SQLite fixture from `tests/emails/conftest.py`.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.emails.automation_ledger_models import AutomationSendLedger
from src.emails.automation_models import EmailAutomation
from src.emails.automation_runner import run_automations_check
from src.emails.email_template_models import EmailTemplate
from src.emails.models import EmailSendLog
from src.schools.models import Contact, School
from src.workshops.models import PortalMapping, Webinar, Workshop

SCHOOL_ID = uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee")
OPTED_IN_CONTACT_ID = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
OPTED_OUT_CONTACT_ID = uuid.UUID("aaaaaaaa-1111-1111-1111-111111111111")
WORKSHOP_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
WEBINAR_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
TEMPLATE_ID = uuid.UUID("55555555-4444-4444-4444-444444444444")
AUTOMATION_ID = uuid.UUID("66666666-4444-4444-4444-444444444444")


def _seed_common(session, *, automation_enabled: bool, auto_emails: bool) -> None:
    session.add(School(id=SCHOOL_ID, name="Test Academy", slug="test-academy", is_current_customer=True))
    session.add(
        Contact(
            id=OPTED_IN_CONTACT_ID if auto_emails else OPTED_OUT_CONTACT_ID,
            school_id=SCHOOL_ID,
            email="family@example.com",
            role="hub_user",
            auto_emails=auto_emails,
        )
    )
    session.add(Workshop(id=WORKSHOP_ID, name="College Planning 101"))
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
            category="workshop_automation",
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
            enabled=automation_enabled,
            offset_value=7,
            offset_unit="days",
            offset_direction="before",
            template_id=TEMPLATE_ID,
        )
    )
    session.commit()


@pytest.fixture
def disabled_automation_session(scheduler_sessionmaker):
    session = scheduler_sessionmaker()
    _seed_common(session, automation_enabled=False, auto_emails=True)
    yield session
    session.close()


@pytest.fixture
def opted_out_contact_session(scheduler_sessionmaker):
    session = scheduler_sessionmaker()
    _seed_common(session, automation_enabled=True, auto_emails=False)
    yield session
    session.close()


def test_disabled_automation_sends_zero_emails_even_with_due_workshop(disabled_automation_session):
    sent = run_automations_check(disabled_automation_session)

    assert sent == 0
    logs = disabled_automation_session.query(EmailSendLog).filter(EmailSendLog.source == "pre_workshop").all()
    assert logs == []

    # Disabled automations are never evaluated, so no ledger row is created either.
    ledger_rows = (
        disabled_automation_session.query(AutomationSendLedger)
        .filter(AutomationSendLedger.automation_id == AUTOMATION_ID)
        .all()
    )
    assert ledger_rows == []


def test_opted_out_contact_receives_no_reminder_with_no_override(opted_out_contact_session):
    sent = run_automations_check(opted_out_contact_session)

    assert sent == 0
    logs = opted_out_contact_session.query(EmailSendLog).filter(EmailSendLog.source == "pre_workshop").all()
    assert logs == []

    # The mapping is still ledgered — the automation ran, template resolved,
    # there were just zero eligible (opted-in) recipients in the batch.
    mapping = opted_out_contact_session.query(PortalMapping).filter(PortalMapping.webinar_id == WEBINAR_ID).one()
    ledger_row = (
        opted_out_contact_session.query(AutomationSendLedger)
        .filter(
            AutomationSendLedger.automation_id == AUTOMATION_ID,
            AutomationSendLedger.portal_mapping_id == mapping.id,
        )
        .one_or_none()
    )
    assert ledger_row is not None
