"""Approve/decline via the review token and the admin calendar."""

from __future__ import annotations

import email
from datetime import datetime, timedelta, timezone

import pytest

from src.guest_contacts.models import GuestContact
from src.paul_booking import decisions
from src.paul_booking.models import PaulMeetingRequest, PaulMeetingRequestBlock
from src.paul_booking.review_token import make_review_token
from tests.paul_booking.conftest import first_slots, post_request, zoom_error

REVIEW = "/api/v1/paul-booking/review"
ADMIN = "/api/v1/admin/paul-booking/requests"


def _make_request(client, index: int = 5, duration: int = 30, email_addr: str = "ana@example.com") -> str:
    slot = first_slots(client, duration)[index]
    assert post_request(client, slot, duration, email=email_addr).status_code == 202
    db = client._session_local()
    try:
        return str(db.query(PaulMeetingRequest).order_by(PaulMeetingRequest.created_at.desc()).first().id)
    finally:
        db.close()


def _row(client, request_id: str) -> PaulMeetingRequest:
    db = client._session_local()
    try:
        import uuid

        return db.get(PaulMeetingRequest, uuid.UUID(request_id))
    finally:
        db.close()


def _update(client, request_id: str, **fields) -> None:
    import uuid

    db = client._session_local()
    try:
        row = db.get(PaulMeetingRequest, uuid.UUID(request_id))
        for k, v in fields.items():
            setattr(row, k, v)
        db.commit()
    finally:
        db.close()


def _token(client, request_id: str) -> str:
    row = _row(client, request_id)
    return make_review_token(row.id, row.expires_at)


def _sent(ses) -> list[email.message.Message]:
    return [email.message_from_bytes(c["RawMessage"]["Data"]) for c in ses.calls]


def test_review_get_shows_request_and_changes_nothing(booking_on, zoom_create):
    rid = _make_request(booking_on)
    resp = booking_on.get(f"{REVIEW}/{_token(booking_on, rid)}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == rid and body["status"] == "pending" and body["lapsed"] is False
    assert body["host_timezone"] == "America/Los_Angeles"
    assert body["contact"]["email"] == "ana@example.com"
    assert resp.headers["cache-control"].startswith("no-store")
    assert _row(booking_on, rid).status == "pending" and zoom_create.calls == []


def test_approve_via_token_creates_meeting_and_emails_both_sides(booking_on, zoom_create, _no_real_email):
    rid = _make_request(booking_on)
    _no_real_email.calls.clear()
    resp = booking_on.post(f"{REVIEW}/{_token(booking_on, rid)}/approve")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "approved" and body["zoom_join_url"] == "https://zoom.example/j/987654321"
    assert len(zoom_create.calls) == 1
    call = zoom_create.calls[0]
    assert call["duration_minutes"] == 30 and "Lincoln High School" in call["topic"]
    assert call["timezone_name"] == "America/Los_Angeles"

    row = _row(booking_on, rid)
    assert row.status == "approved" and row.zoom_meeting_id == "987654321" and row.decided_at
    db = booking_on._session_local()
    assert db.query(GuestContact).one().resolved_at is not None
    assert db.query(PaulMeetingRequestBlock).count() == 1  # still held
    db.close()

    messages = _sent(_no_real_email)
    assert sorted(m["To"] for m in messages) == ["ana@example.com", "paul.martin@collegemoneymethod.com"]
    for m in messages:
        assert m.get_content_type() == "multipart/mixed"
        parts = m.get_payload()
        assert parts[0].get_content_type() == "multipart/alternative"
        assert parts[1].get_content_type() == "text/calendar"
        assert parts[1].get_filename().endswith(".ics")


def test_approve_via_admin_endpoint(booking_on, zoom_create):
    rid = _make_request(booking_on)
    resp = booking_on.post(f"{ADMIN}/{rid}/approve")
    assert resp.status_code == 200 and resp.json()["status"] == "approved"
    assert len(zoom_create.calls) == 1
    assert booking_on.post(f"{ADMIN}/00000000-0000-0000-0000-000000000000/approve").status_code == 404


def test_double_approve_creates_one_meeting(booking_on, zoom_create):
    rid = _make_request(booking_on)
    token = _token(booking_on, rid)
    assert booking_on.post(f"{REVIEW}/{token}/approve").status_code == 200
    again = booking_on.post(f"{REVIEW}/{token}/approve")
    assert again.status_code == 409 and again.json()["status"] == "approved"
    assert booking_on.post(f"{ADMIN}/{rid}/approve").status_code == 409
    assert len(zoom_create.calls) == 1


def test_fresh_approving_claim_blocks_but_stale_claim_is_retried(booking_on, zoom_create):
    rid = _make_request(booking_on)
    now = datetime.now(timezone.utc)
    _update(booking_on, rid, status="approving", claimed_at=now - timedelta(seconds=20))
    blocked = booking_on.post(f"{ADMIN}/{rid}/approve")
    assert blocked.status_code == 409 and blocked.json()["status"] == "approving"
    assert zoom_create.calls == []

    _update(booking_on, rid, claimed_at=now - timedelta(minutes=5))
    assert booking_on.post(f"{ADMIN}/{rid}/approve").status_code == 200
    assert len(zoom_create.calls) == 1


def test_stale_claim_past_reply_by_can_still_be_approved(booking_on, zoom_create):
    rid = _make_request(booking_on)
    now = datetime.now(timezone.utc)
    _update(booking_on, rid, status="approving", claimed_at=now - timedelta(minutes=5),
            expires_at=now - timedelta(minutes=1))
    resp = booking_on.post(f"{ADMIN}/{rid}/approve")
    assert resp.status_code == 200 and resp.json()["status"] == "approved"
    assert len(zoom_create.calls) == 1


def test_stale_claim_past_reply_by_can_be_declined_and_frees_slot(booking_on, zoom_create):
    slot = first_slots(booking_on)[5]
    rid = _make_request(booking_on)
    now = datetime.now(timezone.utc)
    _update(booking_on, rid, status="approving", claimed_at=now - timedelta(minutes=5),
            expires_at=now - timedelta(minutes=1))
    assert slot not in first_slots(booking_on)  # a stuck claim still holds the time
    resp = booking_on.post(f"{ADMIN}/{rid}/decline", json={})
    assert resp.status_code == 200 and resp.json()["status"] == "declined"
    assert slot in first_slots(booking_on)
    assert zoom_create.calls == []


def test_fresh_claim_cannot_be_declined(booking_on):
    rid = _make_request(booking_on)
    _update(booking_on, rid, status="approving", claimed_at=datetime.now(timezone.utc) - timedelta(seconds=10))
    resp = booking_on.post(f"{ADMIN}/{rid}/decline", json={})
    assert resp.status_code == 409 and resp.json()["status"] == "approving"


def test_stale_claim_for_a_call_already_started_cannot_be_approved(booking_on, zoom_create):
    rid = _make_request(booking_on)
    now = datetime.now(timezone.utc)
    _update(booking_on, rid, status="approving", claimed_at=now - timedelta(minutes=5),
            start_at=now - timedelta(minutes=10), expires_at=now - timedelta(hours=13))
    assert booking_on.post(f"{ADMIN}/{rid}/approve").status_code == 409
    assert zoom_create.calls == []
    assert booking_on.post(f"{ADMIN}/{rid}/decline", json={}).status_code == 200


def test_slow_approval_does_not_overwrite_a_decline_made_meanwhile(booking_on, zoom_create, monkeypatch):
    rid = _make_request(booking_on)
    real = zoom_create.__call__

    def decline_during_zoom(**kwargs):
        # The claim goes stale mid-call and the admin declines it.
        _update(booking_on, rid, status="declined", claimed_at=None)
        return real(**kwargs)

    monkeypatch.setattr(decisions, "create_paul_meeting", decline_during_zoom)
    resp = booking_on.post(f"{ADMIN}/{rid}/approve")
    assert resp.status_code == 409 and resp.json()["status"] == "declined"
    row = _row(booking_on, rid)
    assert row.status == "declined" and row.zoom_meeting_id is None


def test_zoom_failure_is_502_and_stays_pending_then_retry_works(booking_on, zoom_create):
    rid = _make_request(booking_on)
    zoom_create.error = zoom_error(500)
    resp = booking_on.post(f"{ADMIN}/{rid}/approve")
    assert resp.status_code == 502
    row = _row(booking_on, rid)
    assert row.status == "pending" and row.zoom_meeting_id is None
    db = booking_on._session_local()
    assert db.query(PaulMeetingRequestBlock).count() == 1  # hold kept
    assert db.query(GuestContact).one().resolved_at is None
    db.close()

    zoom_create.error = None
    assert booking_on.post(f"{ADMIN}/{rid}/approve").status_code == 200
    assert _row(booking_on, rid).status == "approved"


def test_unexpected_error_during_zoom_also_reverts_to_pending(booking_on, zoom_create):
    rid = _make_request(booking_on)
    zoom_create.error = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        booking_on.post(f"{ADMIN}/{rid}/approve")
    assert _row(booking_on, rid).status == "pending"


def test_decline_frees_slot_and_emails_visitor(booking_on, zoom_create, _no_real_email):
    slot = first_slots(booking_on)[5]
    rid = _make_request(booking_on)
    assert slot not in first_slots(booking_on)
    _no_real_email.calls.clear()

    resp = booking_on.post(f"{REVIEW}/{_token(booking_on, rid)}/decline", json={"note": "Out that day"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "declined" and body["decline_note"] == "Out that day"
    assert slot in first_slots(booking_on)
    db = booking_on._session_local()
    assert db.query(PaulMeetingRequestBlock).count() == 0
    assert db.query(GuestContact).one().resolved_at is not None
    db.close()
    (sent,) = _sent(_no_real_email)
    assert sent["To"] == "ana@example.com"
    assert "Out that day" in str(sent.get_payload()[0].get_payload(decode=True), "utf-8")
    assert zoom_create.calls == []

    again = booking_on.post(f"{ADMIN}/{rid}/decline", json={})
    assert again.status_code == 409 and again.json()["status"] == "declined"


def test_decline_via_admin_with_no_body(booking_on):
    rid = _make_request(booking_on)
    resp = booking_on.post(f"{ADMIN}/{rid}/decline")
    assert resp.status_code == 200 and resp.json()["decline_note"] is None


def test_tampered_or_unknown_token_is_404(booking_on):
    rid = _make_request(booking_on)
    token = _token(booking_on, rid)
    flipped = token[:-2] + ("AA" if not token.endswith("AA") else "BB")
    for bad in (flipped, "garbage", "!!!"):
        assert booking_on.get(f"{REVIEW}/{bad}").status_code == 404
        assert booking_on.post(f"{REVIEW}/{bad}/approve").status_code == 404
        assert booking_on.post(f"{REVIEW}/{bad}/decline").status_code == 404
    # A well-signed token for a request that does not exist is also 404.
    import uuid

    ghost = make_review_token(uuid.uuid4(), datetime.now(timezone.utc) + timedelta(hours=5))
    assert booking_on.get(f"{REVIEW}/{ghost}").status_code == 404


def test_token_with_wrong_expiry_is_404(booking_on):
    rid = _make_request(booking_on)
    row = _row(booking_on, rid)
    forged = make_review_token(row.id, row.expires_at + timedelta(days=30))
    assert booking_on.get(f"{REVIEW}/{forged}").status_code == 404


def test_expired_token_shows_lapsed_but_cannot_act(booking_on, zoom_create):
    rid = _make_request(booking_on)
    _update(booking_on, rid, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    # The old epoch no longer matches the stored one, so re-sign for the new expiry.
    token = _token(booking_on, rid)
    review = booking_on.get(f"{REVIEW}/{token}")
    assert review.status_code == 200
    assert review.json()["status"] == "pending" and review.json()["lapsed"] is True
    assert booking_on.post(f"{REVIEW}/{token}/approve").status_code == 410
    assert booking_on.post(f"{REVIEW}/{token}/decline").status_code == 410
    assert zoom_create.calls == []


def test_expired_link_still_shows_a_decided_request(booking_on, zoom_create):
    rid = _make_request(booking_on)
    assert booking_on.post(f"{REVIEW}/{_token(booking_on, rid)}/approve").status_code == 200
    _update(booking_on, rid, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    token = _token(booking_on, rid)
    review = booking_on.get(f"{REVIEW}/{token}")
    assert review.status_code == 200
    assert review.json()["status"] == "approved"
    assert review.json()["zoom_join_url"]
    assert booking_on.post(f"{REVIEW}/{token}/approve").status_code == 409
    assert len(zoom_create.calls) == 1


def test_approve_after_lapse_is_410_via_admin(booking_on, zoom_create):
    rid = _make_request(booking_on)
    _update(booking_on, rid, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    assert booking_on.post(f"{ADMIN}/{rid}/approve").status_code == 410
    assert booking_on.post(f"{ADMIN}/{rid}/decline").status_code in (200, 410)
    assert zoom_create.calls == []


def test_admin_decisions_require_super_admin(booking_on):
    import uuid

    from src.auth.deps import get_current_user
    from src.auth.schemas import CurrentUser
    from src.main import app

    rid = _make_request(booking_on)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=uuid.uuid4(), role="viewer", school_id=None)
    assert booking_on.post(f"{ADMIN}/{rid}/approve").status_code == 403
    assert booking_on.post(f"{ADMIN}/{rid}/decline").status_code == 403
