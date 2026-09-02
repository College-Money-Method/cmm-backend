"""A rescheduled webinar must mail its counselors again for the new date.

The send ledger is the only dedupe signal the runner has, so once a reminder
has gone out the mapping is excluded forever — correct while the date holds,
silently wrong the moment the session moves. `PATCH /webinars/{id}` therefore
clears the ledger for the webinar's mappings when the move is a real
reschedule, and only then.

End-to-end here on purpose: the guarantee is "the counselor gets a second
reminder carrying the new date", which spans the admin PATCH and the runner. A
ledger-level assertion alone would pass even if the runner never re-sent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.client import get_supabase
from src.db.deps import get_db
from src.emails.automation_ledger_models import AutomationSendLedger
from src.emails.automation_models import EmailAutomation
from src.emails.automation_runner import run_automations_check
from src.emails.email_template_models import EmailTemplate
from src.emails.models import EmailSendLog
from src.main import app
from src.schools.models import Contact, School
from src.workshops.models import PortalMapping, Webinar, Workshop

ADMIN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
SCHOOL_ID = uuid.UUID("cccccccc-cccc-cccc-cccc-cccccccccccc")
CONTACT_ID = uuid.UUID("dddddddd-dddd-dddd-dddd-dddddddddddd")
WORKSHOP_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
WEBINAR_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
TEMPLATE_ID = uuid.UUID("33333333-2222-2222-2222-222222222222")
AUTOMATION_ID = uuid.UUID("44444444-2222-2222-2222-222222222222")

# The automation fires 7 days before the start, with a 2-day catch-up, so the
# runner treats a start anywhere in [now+5d, now+7d] as due. Both the original
# and the rescheduled date sit inside it — otherwise a re-armed automation
# would simply be waiting for its window and prove nothing.
ORIGINAL_START = datetime.now(timezone.utc) + timedelta(days=6)
MOVED_START = ORIGINAL_START - timedelta(hours=19)


@pytest.fixture
def sessions(scheduler_sessionmaker):
    """Seeded DB plus an admin client, both on the same in-memory engine."""
    seed = scheduler_sessionmaker()
    seed.add(School(id=SCHOOL_ID, name="Test High", slug="test-high", is_current_customer=True))
    seed.add(
        Contact(
            id=CONTACT_ID,
            school_id=SCHOOL_ID,
            email="counselor@example.com",
            role="hub_user",
            auto_emails=True,
        )
    )
    seed.add(Workshop(id=WORKSHOP_ID, name="FAFSA Basics"))
    seed.add(
        Webinar(
            id=WEBINAR_ID,
            workshop_id=WORKSHOP_ID,
            start_datetime=ORIGINAL_START,
            end_datetime=ORIGINAL_START + timedelta(hours=1),
            registration_url="https://zoom.example.com/register",
        )
    )
    seed.add(
        EmailTemplate(
            id=TEMPLATE_ID,
            category="workshop",
            name="Global announcement",
            subject="Reminder: {{workshop_name}}",
            body_json='{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"See you on {{date}}."}]}]}',
        )
    )
    seed.add(PortalMapping(id=uuid.uuid4(), school_id=SCHOOL_ID, webinar_id=WEBINAR_ID))
    seed.add(
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
    seed.commit()
    seed.close()

    def override_get_db():
        db = scheduler_sessionmaker()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_USER_ID, email="admin@collegemoneymethod.com", role="super_admin"
    )
    yield scheduler_sessionmaker, TestClient(app)
    app.dependency_overrides.clear()


def _run_check(sessionmaker_) -> int:
    """Run the scheduler on its own session, as the background job does."""
    db = sessionmaker_()
    try:
        return run_automations_check(db)
    finally:
        db.close()


def _reminder_count(sessionmaker_) -> int:
    db = sessionmaker_()
    try:
        return db.query(EmailSendLog).filter(EmailSendLog.source == "pre_workshop").count()
    finally:
        db.close()


def _ledger_count(sessionmaker_) -> int:
    db = sessionmaker_()
    try:
        return db.query(AutomationSendLedger).count()
    finally:
        db.close()


def _patch_start(client: TestClient, start: datetime, **extra):
    return client.patch(
        f"/api/v1/workshops/webinars/{WEBINAR_ID}",
        json={
            "start_datetime": start.isoformat(),
            "end_datetime": (start + timedelta(hours=1)).isoformat(),
            **extra,
        },
    )


def test_reschedule_resends_the_reminder_for_the_new_date(sessions):
    sessionmaker_, client = sessions
    assert _run_check(sessionmaker_) == 1, "reminder goes out for the original date"
    assert _ledger_count(sessionmaker_) == 1
    assert _run_check(sessionmaker_) == 0, "and is not repeated while the date holds"

    resp = _patch_start(client, MOVED_START)
    assert resp.status_code == 200
    body = resp.json()
    assert body["rearmed_automation_sends"] == 1, "the admin is told what will be mailed again"
    assert body["rescheduled_at"] is not None
    assert _ledger_count(sessionmaker_) == 0, "the claim is cleared, not the history"
    assert _reminder_count(sessionmaker_) == 1, "already-sent mail stays in the audit trail"

    assert _run_check(sessionmaker_) == 1, "the reminder is sent again for the new date"
    assert _reminder_count(sessionmaker_) == 2
    assert _run_check(sessionmaker_) == 0, "exactly once for the new date, too"


def test_small_nudge_leaves_the_ledger_alone(sessions):
    """Below the materiality threshold nobody should be mailed twice."""
    sessionmaker_, client = sessions
    assert _run_check(sessionmaker_) == 1

    resp = _patch_start(client, ORIGINAL_START - timedelta(minutes=20))
    assert resp.status_code == 200
    assert resp.json()["rearmed_automation_sends"] == 0
    assert _ledger_count(sessionmaker_) == 1

    assert _run_check(sessionmaker_) == 0
    assert _reminder_count(sessionmaker_) == 1


def test_correcting_a_past_date_mails_nobody(sessions):
    """The override says "this already happened, I am fixing the record" — a
    session in the past has no reminder left to send."""
    sessionmaker_, client = sessions
    assert _run_check(sessionmaker_) == 1

    resp = _patch_start(client, datetime.now(timezone.utc) - timedelta(days=30), allow_past_datetime=True)
    assert resp.status_code == 200
    assert resp.json()["rearmed_automation_sends"] == 0
    assert resp.json()["rescheduled_at"] is None
    assert _ledger_count(sessionmaker_) == 1

    assert _run_check(sessionmaker_) == 0
    assert _reminder_count(sessionmaker_) == 1


def test_only_the_moved_webinars_ledger_is_cleared(sessions):
    """Re-arming is scoped to the session that moved: another webinar's claims
    are untouched, or one reschedule would re-mail the whole cycle."""
    sessionmaker_, client = sessions
    other_webinar_id = uuid.uuid4()
    other_mapping_id = uuid.uuid4()
    db = sessionmaker_()
    try:
        db.add(
            Webinar(
                id=other_webinar_id,
                workshop_id=WORKSHOP_ID,
                start_datetime=ORIGINAL_START + timedelta(days=30),
            )
        )
        db.flush()
        db.add(
            PortalMapping(id=other_mapping_id, school_id=SCHOOL_ID, webinar_id=other_webinar_id)
        )
        db.flush()
        db.add(
            AutomationSendLedger(automation_id=AUTOMATION_ID, portal_mapping_id=other_mapping_id)
        )
        db.commit()
    finally:
        db.close()

    assert _run_check(sessionmaker_) == 1
    assert _ledger_count(sessionmaker_) == 2

    resp = _patch_start(client, MOVED_START)
    assert resp.status_code == 200
    assert resp.json()["rearmed_automation_sends"] == 1

    db = sessionmaker_()
    try:
        remaining = db.query(AutomationSendLedger).all()
        assert [row.portal_mapping_id for row in remaining] == [other_mapping_id]
    finally:
        db.close()
