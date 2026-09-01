"""Senders configured to mail without any unsubscribe mechanism.

`settings.ses_no_unsubscribe_senders` names From addresses whose mail carries
neither the visible footer link nor the one-click `List-Unsubscribe` header.
It exists for one-to-one style mail to a small, already-opted-in audience.

Both halves come off a single decision — `unsubscribe_url=None` — so the tests
below check the two halves together on every send path: they must never drift
apart, and a sender that is *not* on the list must still get both.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from src.config import settings
from src.emails.automation_models import EmailAutomation
from src.emails.automation_runner import run_automations_check
from src.emails.broadcast_models import Broadcast
from src.emails.broadcast_send import send_to_contacts
from src.emails.email_template_models import EmailTemplate
from src.emails.models import EmailSendLog
from src.emails.sender import no_unsubscribe_senders, sender_omits_unsubscribe
from src.emails.ses_client import _build_raw_message
from src.schools.models import Contact, School
from src.workshops.models import PortalMapping, Webinar, Workshop

QUIET_SENDER = "paul.martin@collegemoneymethod.com"
NORMAL_SENDER = "newsflash@collegemoneymethod.com"

BODY = json.dumps(
    {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}]}
)


# ── The setting itself ──────────────────────────────────────────────────────


def test_configured_sender_is_matched_case_insensitively():
    assert sender_omits_unsubscribe(QUIET_SENDER)
    assert sender_omits_unsubscribe(QUIET_SENDER.upper())
    assert sender_omits_unsubscribe(f"  {QUIET_SENDER}  ")
    assert not sender_omits_unsubscribe(NORMAL_SENDER)


def test_a_blank_setting_means_everyone_keeps_their_unsubscribe(monkeypatch):
    """The inverse of the domain allowlist: this list REMOVES a safeguard, so a
    misconfigured env must fall back to "everyone gets one", never "nobody does"."""
    monkeypatch.setattr(settings, "ses_no_unsubscribe_senders", "")

    assert no_unsubscribe_senders() == set()
    assert not sender_omits_unsubscribe(QUIET_SENDER)


def test_no_sender_on_the_send_falls_back_to_the_configured_default(monkeypatch):
    """A send with no explicit From goes out as `ses_from_email`, so that is the
    address the list must be checked against."""
    monkeypatch.setattr(settings, "ses_from_email", QUIET_SENDER)
    assert sender_omits_unsubscribe(None)

    monkeypatch.setattr(settings, "ses_from_email", NORMAL_SENDER)
    assert not sender_omits_unsubscribe(None)


def test_no_unsubscribe_url_means_no_list_unsubscribe_header():
    """The header half of the decision, at the MIME layer."""
    quiet = _build_raw_message(["a@example.com"], "Subject", "<p>hi</p>", "hi").decode()
    assert "List-Unsubscribe" not in quiet

    normal = _build_raw_message(
        ["a@example.com"], "Subject", "<p>hi</p>", "hi", unsubscribe_url="https://x.com/u?t=tok"
    ).decode()
    assert "List-Unsubscribe: <https://x.com/u?t=tok>" in normal
    assert "List-Unsubscribe-Post: List-Unsubscribe=One-Click" in normal


# ── Broadcast send path ─────────────────────────────────────────────────────


def _seed_broadcast_audience(session, sender_email: str) -> Broadcast:
    school_id, contact_id = uuid.uuid4(), uuid.uuid4()
    session.add(School(id=school_id, name="Test Academy", slug=f"s-{school_id.hex[:8]}", is_current_customer=True))
    session.add(
        Contact(
            id=contact_id,
            school_id=school_id,
            email="counselor@example.com",
            first_name="Caroline",
            role="hub_user",
            auto_emails=True,
        )
    )
    broadcast = Broadcast(
        id=uuid.uuid4(),
        subject="Checking in",
        body_json=BODY,
        sender_email=sender_email,
        created_by=uuid.uuid4(),
    )
    session.add(broadcast)
    session.commit()
    return broadcast


@pytest.mark.parametrize(
    ("sender_email", "expect_unsubscribe"),
    [(QUIET_SENDER, False), (NORMAL_SENDER, True)],
)
def test_broadcast_unsubscribe_follows_the_sender(
    scheduler_sessionmaker, sender_email, expect_unsubscribe
):
    session = scheduler_sessionmaker()
    broadcast = _seed_broadcast_audience(session, sender_email)
    contacts = session.query(Contact).all()
    school = session.query(School).one()

    send_to_contacts(session, broadcast, contacts, school)

    log = session.query(EmailSendLog).filter(EmailSendLog.broadcast_id == broadcast.id).one()
    assert ("Unsubscribe" in (log.rendered_html or "")) is expect_unsubscribe
    session.close()


# ── Automation send path ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("sender_email", "expect_unsubscribe"),
    [(QUIET_SENDER, False), (NORMAL_SENDER, True)],
)
def test_automation_unsubscribe_follows_the_sender(
    scheduler_sessionmaker, sender_email, expect_unsubscribe
):
    session = scheduler_sessionmaker()
    template_id = uuid.uuid4()
    session.add(
        EmailTemplate(
            id=template_id,
            category="workshop",
            name="Follow-up",
            subject="Re: {{workshop_name}}",
            body_json=BODY,
        )
    )
    school_id, contact_id, workshop_id, webinar_id, mapping_id, automation_id = (
        uuid.uuid4() for _ in range(6)
    )
    now = datetime.now(timezone.utc)
    session.add(School(id=school_id, name="Test Academy", slug=f"s-{school_id.hex[:8]}", is_current_customer=True))
    session.add(
        Contact(id=contact_id, school_id=school_id, email="family@example.com", role="hub_user", auto_emails=True)
    )
    session.add(Workshop(id=workshop_id, name="College Planning 101"))
    session.add(
        Webinar(
            id=webinar_id,
            workshop_id=workshop_id,
            start_datetime=now - timedelta(days=8),
            registration_url="https://zoom.example.com/register",
        )
    )
    session.add(PortalMapping(id=mapping_id, school_id=school_id, webinar_id=webinar_id))
    session.add(
        EmailAutomation(
            id=automation_id,
            name="Post-Workshop Follow-up",
            type="post_workshop_reminder",
            enabled=True,
            offset_value=7,
            offset_unit="days",
            offset_direction="after",
            template_id=template_id,
            sender_email=sender_email,
        )
    )
    session.commit()

    assert run_automations_check(session) == 1

    log = session.query(EmailSendLog).filter(EmailSendLog.automation_id == automation_id).one()
    assert ("Unsubscribe" in (log.rendered_html or "")) is expect_unsubscribe
    session.close()
