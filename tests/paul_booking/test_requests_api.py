"""Public slots + request flow, holds, spam, and the admin time-off/list endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.guest_contacts.models import GuestContact
from src.integrations.zoom import ZoomApiError
from src.paul_booking.models import (
    PaulMeetingRequest,
    PaulMeetingRequestBlock,
    PaulTimeOff,
)
from tests.paul_booking.conftest import (
    first_slots,
    post_request,
)

SLOTS = "/api/v1/paul-booking/slots"
ADMIN = "/api/v1/admin/paul-booking"


def _rows(client, model):
    db = client._session_local()
    try:
        return db.query(model).all()
    finally:
        db.close()


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_slots_shape_and_default_timezone(booking_on):
    data = booking_on.get(SLOTS, params={"duration": 60}).json()
    assert data["host_timezone"] == "America/Los_Angeles"
    assert data["duration"] == 60
    assert data["slots"] and data["slots"][0].endswith("Z")
    first = datetime.fromisoformat(data["slots"][0].replace("Z", "+00:00"))
    assert first >= datetime.now(timezone.utc) + timedelta(hours=24)


def test_slots_rejects_unknown_duration(booking_on):
    assert booking_on.get(SLOTS, params={"duration": 45}).status_code == 422


def test_disabled_booking_is_503_for_slots_and_requests(client):
    assert client.get(SLOTS, params={"duration": 30}).status_code == 503
    resp = post_request(client, "2026-12-01T10:00:00Z")
    assert resp.status_code == 503


def test_zoom_unavailable_is_503(booking_on, zoom_busy):
    zoom_busy.error = ZoomApiError("Zoom is not configured")
    assert booking_on.get(SLOTS, params={"duration": 30}).status_code == 503
    start = "2026-12-01T10:00:00Z"
    assert post_request(booking_on, start).status_code == 503


def test_zoom_busy_blocks_slot(booking_on, zoom_busy):
    slot = first_slots(booking_on)[5]
    start = datetime.fromisoformat(slot.replace("Z", "+00:00"))
    zoom_busy.intervals = [(start - timedelta(minutes=10), start + timedelta(minutes=10))]
    assert slot not in first_slots(booking_on)
    assert post_request(booking_on, slot).status_code == 422


def test_request_creates_hold_and_sends_two_emails(booking_on, _no_real_email):
    slot = first_slots(booking_on, 60)[5]
    resp = post_request(booking_on, slot, 60)
    assert resp.status_code == 202
    body = resp.json()
    assert set(body) == {"status", "start", "duration", "expires_at"}
    assert body["status"] == "pending" and body["duration"] == 60

    (req,) = _rows(booking_on, PaulMeetingRequest)
    assert req.status == "pending" and len(_rows(booking_on, PaulMeetingRequest)) == 1
    assert len(_rows(booking_on, PaulMeetingRequestBlock)) == 2
    (contact,) = _rows(booking_on, GuestContact)
    assert contact.source == "paul_booking" and contact.is_spam is False
    recipients = sorted(call["Destinations"][0] for call in _no_real_email.calls)
    assert recipients == ["ana@example.com", "paul.martin@collegemoneymethod.com"]
    assert slot not in first_slots(booking_on, 60)


def test_overlapping_sixty_vs_thirty_is_409(booking_on):
    slots = first_slots(booking_on, 60)
    start = datetime.fromisoformat(slots[5].replace("Z", "+00:00"))
    assert post_request(booking_on, _iso(start), 60, email="a@example.com", ip="198.51.100.1").status_code == 202
    # Second half of the 60-minute hold, and a 60 starting half an hour earlier.
    assert post_request(booking_on, _iso(start + timedelta(minutes=30)), 30, email="b@example.com", ip="198.51.100.2").status_code == 409
    assert post_request(booking_on, _iso(start - timedelta(minutes=30)), 60, email="c@example.com", ip="198.51.100.3").status_code == 409
    assert len(_rows(booking_on, PaulMeetingRequest)) == 1
    assert len(_rows(booking_on, GuestContact)) == 1  # the losers stored nothing


def test_lapsed_hold_is_reclaimed(booking_on):
    slot = first_slots(booking_on)[5]
    assert post_request(booking_on, slot, email="a@example.com", ip="198.51.100.1").status_code == 202
    db = booking_on._session_local()
    old = db.query(PaulMeetingRequest).one()
    old.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.commit()
    old_id = old.id
    db.close()

    assert slot in first_slots(booking_on)  # lapsed hold no longer blocks the slot
    assert post_request(booking_on, slot, email="b@example.com", ip="198.51.100.2").status_code == 202
    blocks = _rows(booking_on, PaulMeetingRequestBlock)
    assert len(blocks) == 1 and blocks[0].request_id != old_id


def test_spam_is_quarantined_without_hold_or_email(booking_on, _no_real_email):
    slot = first_slots(booking_on)[5]
    real = post_request(booking_on, slot, email="real@example.com", ip="198.51.100.9")
    _no_real_email.calls.clear()
    other = first_slots(booking_on)[9]
    spam = post_request(booking_on, other, website="http://spam.example", email="bot@example.com", ip="198.51.100.8")
    assert spam.status_code == 202
    assert set(spam.json()) == set(real.json())
    assert len(_rows(booking_on, PaulMeetingRequest)) == 1
    assert len(_rows(booking_on, PaulMeetingRequestBlock)) == 1
    assert [c.is_spam for c in sorted(_rows(booking_on, GuestContact), key=lambda c: c.email)] == [True, False]
    assert _no_real_email.calls == []
    assert other in first_slots(booking_on)


def test_invalid_slots_are_422(booking_on):
    slot = datetime.fromisoformat(first_slots(booking_on)[5].replace("Z", "+00:00"))
    assert post_request(booking_on, _iso(slot + timedelta(minutes=7))).status_code == 422
    assert post_request(booking_on, _iso(datetime.now(timezone.utc) + timedelta(hours=2))).status_code == 422
    assert post_request(booking_on, _iso(datetime.now(timezone.utc) + timedelta(days=30))).status_code == 422
    assert post_request(booking_on, "2026-12-01T10:00:00", ).status_code == 422  # naive datetime


def test_rate_limit_and_pending_cap(booking_on):
    slots = first_slots(booking_on)
    for i in range(3):
        assert post_request(booking_on, slots[5 + 3 * i], email=f"p{i}@example.com", ip="198.51.100.5").status_code == 202
    assert post_request(booking_on, slots[20], email="p9@example.com", ip="198.51.100.5").status_code == 429
    # One address may hold at most two open requests, even from fresh IPs.
    assert post_request(booking_on, slots[30], email="p0@example.com", ip="198.51.100.6").status_code == 202
    assert post_request(booking_on, slots[40], email="p0@example.com", ip="198.51.100.7").status_code == 429


def test_unknown_visitor_timezone_is_422(booking_on):
    slot = first_slots(booking_on)[5]
    assert post_request(booking_on, slot, time_zone="Mars/Olympus").status_code == 422


def test_time_off_removes_slots_and_reports_overlap(booking_on):
    slot = first_slots(booking_on)[5]
    assert post_request(booking_on, slot).status_code == 202
    day = datetime.fromisoformat(slot.replace("Z", "+00:00")).astimezone(__import__("zoneinfo").ZoneInfo("America/Los_Angeles")).date()

    resp = booking_on.post(f"{ADMIN}/time-off", json={"starts_on": str(day), "ends_on": str(day), "note": "Trip"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["item"]["starts_on"] == str(day) and data["item"]["note"] == "Trip"
    assert [r["contact"]["email"] for r in data["overlapping_requests"]] == ["ana@example.com"]
    # Never auto-declines.
    assert _rows(booking_on, PaulMeetingRequest)[0].status == "pending"
    assert slot not in first_slots(booking_on)

    listed = booking_on.get(f"{ADMIN}/time-off", params={"from": str(day), "to": str(day)}).json()
    assert len(listed) == 1
    assert booking_on.delete(f"{ADMIN}/time-off/{data['item']['id']}").status_code == 204
    assert booking_on.delete(f"{ADMIN}/time-off/{data['item']['id']}").status_code == 404
    assert _rows(booking_on, PaulTimeOff) == []


def test_time_off_rejects_backwards_range(booking_on):
    resp = booking_on.post(f"{ADMIN}/time-off", json={"starts_on": "2026-11-05", "ends_on": "2026-11-01"})
    assert resp.status_code == 422


def test_admin_list_returns_all_statuses_with_lapsed_flag(booking_on):
    slots = first_slots(booking_on)
    post_request(booking_on, slots[5], email="a@example.com", ip="198.51.100.1")
    post_request(booking_on, slots[15], email="b@example.com", ip="198.51.100.2")
    db = booking_on._session_local()
    later = db.query(PaulMeetingRequest).order_by(PaulMeetingRequest.start_at.desc()).first()
    later.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db.commit()
    db.close()
    lo, hi = _iso(datetime.now(timezone.utc)), _iso(datetime.now(timezone.utc) + timedelta(days=30))
    items = booking_on.get(f"{ADMIN}/requests", params={"from": lo, "to": hi}).json()
    assert [i["lapsed"] for i in items] == [False, True]
    assert items[0]["start_at"] < items[1]["start_at"]
    assert items[0]["contact"]["email"] == "a@example.com" and items[0]["status"] == "pending"
    assert set(items[0]) >= {"id", "start_at", "duration_minutes", "visitor_timezone", "status", "lapsed",
                             "expires_at", "created_at", "decided_at", "decline_note", "zoom_join_url", "contact"}


def test_admin_routes_require_super_admin(booking_on):
    import uuid

    from src.auth.deps import get_current_user
    from src.auth.schemas import CurrentUser
    from src.main import app

    app.dependency_overrides[get_current_user] = lambda: CurrentUser(user_id=uuid.uuid4(), role="viewer", school_id=None)
    assert booking_on.get(f"{ADMIN}/time-off", params={"from": "2026-11-01", "to": "2026-11-02"}).status_code == 403
    assert booking_on.get(SLOTS, params={"duration": 30}).status_code == 200  # public stays public


def test_slots_are_rate_limited_per_address(booking_on):
    for _ in range(30):
        assert booking_on.get(SLOTS, params={"duration": 30}, headers={"x-forwarded-for": "198.51.100.9"}).status_code == 200
    assert booking_on.get(SLOTS, params={"duration": 30}, headers={"x-forwarded-for": "198.51.100.9"}).status_code == 429
    assert booking_on.get(SLOTS, params={"duration": 30}, headers={"x-forwarded-for": "198.51.100.10"}).status_code == 200


def test_newlines_in_names_are_collapsed_so_pauls_email_still_sends(booking_on, _no_real_email):
    import email

    slot = first_slots(booking_on)[5]
    resp = post_request(booking_on, slot, first_name="Ana\r\nBcc: x@example.com", school_name="Lincoln\nHigh\x00")
    assert resp.status_code == 202
    (contact,) = _rows(booking_on, GuestContact)
    assert contact.first_name == "Ana Bcc: x@example.com" and contact.school_name == "Lincoln High"
    subjects = [email.message_from_bytes(c["RawMessage"]["Data"])["Subject"] for c in _no_real_email.calls]
    assert len(subjects) == 2 and any(s.startswith("Call request: Ana Bcc") for s in subjects)
    assert all("\n" not in s for s in subjects)


def test_blank_name_is_422_and_blank_role_is_dropped(booking_on):
    slot = first_slots(booking_on)[5]
    assert post_request(booking_on, slot, first_name=" \n\t ").status_code == 422
    assert post_request(booking_on, slot, role="  ").status_code == 202
    (contact,) = _rows(booking_on, GuestContact)
    assert contact.role is None
