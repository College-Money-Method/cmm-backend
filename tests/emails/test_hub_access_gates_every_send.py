"""Counselor Hub access is a precondition for receiving any CMM email.

A school's contact list holds everyone the school named, including staff who
were never given a hub login. Both send paths — the scheduler's workshop
automations and admin broadcasts — must reach only the contacts who actually
have hub access, or a school directory turns into a mailing list.

Covers the runner, the broadcast audience resolver, and the explicit
recipient-id resolver (an admin's hand-picked list is still server-checked).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.emails.audience import resolve_audience, resolve_contacts_by_ids
from src.emails.automation_models import EmailAutomation
from src.emails.automation_runner import run_automations_check
from src.emails.email_template_models import EmailTemplate
from src.emails.models import EmailSendLog
from src.schools.models import Contact, School
from src.workshops.models import PortalMapping, Webinar, Workshop
from tests.emails.conftest import grant_hub_access

SCHOOL_ID = uuid.UUID("cacacaca-caca-caca-caca-cacacacacaca")
HUB_CONTACT_ID = uuid.UUID("cbcbcbcb-cbcb-cbcb-cbcb-cbcbcbcbcbcb")
NO_HUB_CONTACT_ID = uuid.UUID("cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd")
WORKSHOP_ID = uuid.UUID("cecececa-cece-cece-cece-cecececececa")
WEBINAR_ID = uuid.UUID("cfcfcfcf-cfcf-cfcf-cfcf-cfcfcfcfcfcf")
TEMPLATE_ID = uuid.UUID("dadadada-dada-dada-dada-dadadadadada")
AUTOMATION_ID = uuid.UUID("dbdbdbdb-dbdb-dbdb-dbdb-dbdbdbdbdbdb")


def _seed_two_contacts(session) -> None:
    """One contact with a hub login, one without — both fully opted in, so hub
    access is the only thing separating them."""
    session.add(School(id=SCHOOL_ID, name="Hewitt Academy", slug="hewitt", is_current_customer=True))
    with_hub = Contact(
        id=HUB_CONTACT_ID,
        school_id=SCHOOL_ID,
        email="counselor@example.com",
        first_name="Casey",
        role="Counselor",
        auto_emails=True,
        broadcast_emails=True,
    )
    without_hub = Contact(
        id=NO_HUB_CONTACT_ID,
        school_id=SCHOOL_ID,
        email="frontdesk@example.com",
        first_name="Dana",
        role="Counselor",
        auto_emails=True,
        broadcast_emails=True,
    )
    session.add_all([with_hub, without_hub])
    grant_hub_access(session, with_hub)
    session.commit()


@pytest.fixture
def session(scheduler_sessionmaker):
    db = scheduler_sessionmaker()
    _seed_two_contacts(db)
    yield db
    db.close()


def _seed_due_automation(session) -> None:
    session.add(Workshop(id=WORKSHOP_ID, name="FAFSA Night"))
    session.add(
        Webinar(
            id=WEBINAR_ID,
            workshop_id=WORKSHOP_ID,
            # 7-day "before" offset against a start 6 days out: fire_at landed
            # yesterday, inside the runner's 2-day catch-up window.
            start_datetime=datetime.now(timezone.utc) + timedelta(days=6),
            registration_url="https://zoom.example.com/register",
        )
    )
    session.add(
        EmailTemplate(
            id=TEMPLATE_ID,
            category="workshop",
            name="Reminder",
            subject="Reminder: {{workshop_name}}",
            body_json='{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"See you at {{workshop_name}}."}]}]}',
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


def test_automation_reaches_only_the_contact_with_hub_access(session):
    _seed_due_automation(session)

    assert run_automations_check(session) == 1

    logs = session.query(EmailSendLog).filter(EmailSendLog.source == "pre_workshop").all()
    assert [log.recipient_email for log in logs] == ["counselor@example.com"]


def test_broadcast_audience_excludes_the_contact_without_hub_access(session):
    contacts = resolve_audience(session, [], [], "all", "opted_in")

    assert [c.email for c in contacts] == ["counselor@example.com"]


def test_opt_in_filter_all_still_cannot_reach_a_contact_without_hub_access(session):
    """"all" relaxes the opt-in dimension only — hub access has no override."""
    contacts = resolve_audience(session, [], [], "all", "all")

    assert [c.email for c in contacts] == ["counselor@example.com"]


def test_explicit_recipient_ids_are_still_checked_for_hub_access(session):
    """An admin hand-picking recipients does not bypass the rule: the id is
    resolved server-side, and a contact with no hub login drops out."""
    contacts = resolve_contacts_by_ids(session, [HUB_CONTACT_ID, NO_HUB_CONTACT_ID])

    assert [c.email for c in contacts] == ["counselor@example.com"]
