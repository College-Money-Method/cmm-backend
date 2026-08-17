"""Dynamic-offset behavior of the automation runner (`automation_runner.py`):

- post-workshop ("after" direction) automations send once past the anchor.
- an `hours` offset_unit is honored (not just `days`).
- "before" automations never fire once the workshop has already started, even
  if the due window math would otherwise match (direction filtering).
- a missing/invalid `template_id` skips the send and leaves NO ledger row, so
  the mapping is retried automatically once a template is configured.

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

TEMPLATE_ID = uuid.uuid4()


def _seed_base(session, *, school_id, contact_id, workshop_id, webinar_id, start_datetime, mapping_id):
    session.add(School(id=school_id, name="Test Academy", slug=f"school-{school_id.hex[:8]}", is_current_customer=True))
    session.add(
        Contact(id=contact_id, school_id=school_id, email="family@example.com", role="hub_user", auto_emails=True)
    )
    session.add(Workshop(id=workshop_id, name="College Planning 101"))
    session.add(
        Webinar(
            id=webinar_id,
            workshop_id=workshop_id,
            start_datetime=start_datetime,
            registration_url="https://zoom.example.com/register",
        )
    )
    session.add(PortalMapping(id=mapping_id, school_id=school_id, webinar_id=webinar_id))


@pytest.fixture
def template(scheduler_sessionmaker):
    """A shared workshop template, seeded once per test session."""
    session = scheduler_sessionmaker()
    session.add(
        EmailTemplate(
            id=TEMPLATE_ID,
            category="workshop",
            name="Automation template",
            subject="Re: {{workshop_name}}",
            body_json='{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"{{workshop_name}} on {{date}}."}]}]}',
        )
    )
    session.commit()
    return session


def test_post_workshop_after_offset_sends_once(template):
    session = template
    school_id, contact_id, workshop_id, webinar_id, mapping_id, automation_id = (uuid.uuid4() for _ in range(6))
    now = datetime.now(timezone.utc)
    # Workshop ended 8 days ago; offset=7 days "after" -> fire_at = start + 7d = 1 day ago (within catch-up).
    _seed_base(
        session,
        school_id=school_id,
        contact_id=contact_id,
        workshop_id=workshop_id,
        webinar_id=webinar_id,
        start_datetime=now - timedelta(days=8),
        mapping_id=mapping_id,
    )
    session.add(
        EmailAutomation(
            id=automation_id,
            name="Post-Workshop Follow-up",
            type="post_workshop_reminder",
            enabled=True,
            offset_value=7,
            offset_unit="days",
            offset_direction="after",
            template_id=TEMPLATE_ID,
        )
    )
    session.commit()

    sent = run_automations_check(session)
    assert sent == 1

    logs = session.query(EmailSendLog).filter(EmailSendLog.source == "post_workshop").all()
    assert len(logs) == 1
    assert logs[0].automation_id == automation_id

    ledger_row = (
        session.query(AutomationSendLedger)
        .filter(AutomationSendLedger.automation_id == automation_id, AutomationSendLedger.portal_mapping_id == mapping_id)
        .one_or_none()
    )
    assert ledger_row is not None


def test_hours_offset_unit_is_honored(template):
    session = template
    school_id, contact_id, workshop_id, webinar_id, mapping_id, automation_id = (uuid.uuid4() for _ in range(6))
    now = datetime.now(timezone.utc)
    # Workshop starts in 2 hours; offset=3 hours "before" -> fire_at = start - 3h = now - 1h (due).
    _seed_base(
        session,
        school_id=school_id,
        contact_id=contact_id,
        workshop_id=workshop_id,
        webinar_id=webinar_id,
        start_datetime=now + timedelta(hours=2),
        mapping_id=mapping_id,
    )
    session.add(
        EmailAutomation(
            id=automation_id,
            name="3-Hour Reminder",
            type="pre_workshop_reminder",
            enabled=True,
            offset_value=3,
            offset_unit="hours",
            offset_direction="before",
            template_id=TEMPLATE_ID,
        )
    )
    session.commit()

    sent = run_automations_check(session)
    assert sent == 1
    logs = session.query(EmailSendLog).filter(EmailSendLog.source == "pre_workshop").all()
    assert len(logs) == 1


def test_before_direction_never_fires_after_workshop_already_started(template):
    session = template
    school_id, contact_id, workshop_id, webinar_id, mapping_id, automation_id = (uuid.uuid4() for _ in range(6))
    now = datetime.now(timezone.utc)
    # Workshop started 1 hour ago; a 1-hour "before" offset places fire_at at
    # now - 2h (inside the catch-up window), but `now < start` must gate it off.
    _seed_base(
        session,
        school_id=school_id,
        contact_id=contact_id,
        workshop_id=workshop_id,
        webinar_id=webinar_id,
        start_datetime=now - timedelta(hours=1),
        mapping_id=mapping_id,
    )
    session.add(
        EmailAutomation(
            id=automation_id,
            name="1-Hour Reminder",
            type="pre_workshop_reminder",
            enabled=True,
            offset_value=1,
            offset_unit="hours",
            offset_direction="before",
            template_id=TEMPLATE_ID,
        )
    )
    session.commit()

    sent = run_automations_check(session)
    assert sent == 0
    logs = session.query(EmailSendLog).filter(EmailSendLog.source == "pre_workshop").all()
    assert logs == []


def test_missing_template_skips_and_leaves_no_ledger_row(scheduler_sessionmaker):
    session = scheduler_sessionmaker()
    school_id, contact_id, workshop_id, webinar_id, mapping_id, automation_id = (uuid.uuid4() for _ in range(6))
    now = datetime.now(timezone.utc)
    _seed_base(
        session,
        school_id=school_id,
        contact_id=contact_id,
        workshop_id=workshop_id,
        webinar_id=webinar_id,
        start_datetime=now + timedelta(days=3),
        mapping_id=mapping_id,
    )
    session.add(
        EmailAutomation(
            id=automation_id,
            name="No Template Yet",
            type="pre_workshop_reminder",
            enabled=True,
            offset_value=7,
            offset_unit="days",
            offset_direction="before",
            template_id=None,
        )
    )
    session.commit()

    sent = run_automations_check(session)
    assert sent == 0
    logs = session.query(EmailSendLog).all()
    assert logs == []

    ledger_rows = (
        session.query(AutomationSendLedger)
        .filter(AutomationSendLedger.automation_id == automation_id)
        .all()
    )
    assert ledger_rows == []
    session.close()
