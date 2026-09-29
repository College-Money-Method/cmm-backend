"""Marking a contact-form enquiry answered.

Replies are sent from a mail client, so nothing can detect them — the admin says
so, and the row remembers. Null ``resolved_at`` means the enquiry is still owed
a reply, which is the state the inbox header counts.
"""

from __future__ import annotations

from src.guest_contacts.models import GuestContact

ENQUIRY = {
    "first_name": "Patricia",
    "last_name": "Davico",
    "email": "patricia@example.com",
    "school_name": "St. Ignatius College Prep",
    "message": (
        "I direct college counselling here and would like to talk about bringing "
        "your financial aid sessions to our school this year."
    ),
}
BOT = {
    "first_name": "ussppyXbAPxyhvUi",
    "last_name": "BDJZjHHdCzIsbpyEIDpPMZsn",
    "email": "buni.q.in783@gmail.com",
    "school_name": "CUDKcLwIRfACLYGlbxLABui",
    "message": "7452959171",
}


def _submit(client, body=None):
    client.post("/api/v1/guest-contacts", json=body or ENQUIRY)
    db = client._session_local()
    try:
        return db.query(GuestContact).order_by(GuestContact.created_at.desc()).first().id
    finally:
        db.close()


def _counts(client):
    return client.get("/api/v1/guest-contacts/counts").json()


def test_a_new_enquiry_starts_unanswered(client):
    _submit(client)
    row = client.get("/api/v1/guest-contacts").json()[0]
    assert row["resolved_at"] is None


def test_marking_it_answered_stamps_the_moment(client):
    gc_id = _submit(client)
    resp = client.patch(f"/api/v1/guest-contacts/{gc_id}/resolved", params={"resolved": True})
    assert resp.status_code == 200
    assert resp.json()["resolved_at"] is not None


def test_it_stays_answered_across_a_reread(client):
    """The stamp is persisted, not just echoed back by the write."""
    gc_id = _submit(client)
    client.patch(f"/api/v1/guest-contacts/{gc_id}/resolved", params={"resolved": True})
    assert client.get(f"/api/v1/guest-contacts/{gc_id}").json()["resolved_at"] is not None


def test_an_answered_enquiry_can_be_reopened(client):
    gc_id = _submit(client)
    client.patch(f"/api/v1/guest-contacts/{gc_id}/resolved", params={"resolved": True})
    resp = client.patch(f"/api/v1/guest-contacts/{gc_id}/resolved", params={"resolved": False})
    assert resp.json()["resolved_at"] is None


def test_answering_does_not_hide_the_row(client):
    """Answered is a state, not an archive — the row stays in the inbox list."""
    gc_id = _submit(client)
    client.patch(f"/api/v1/guest-contacts/{gc_id}/resolved", params={"resolved": True})
    assert len(client.get("/api/v1/guest-contacts").json()) == 1


def test_the_outstanding_count_tracks_both_directions(client):
    gc_id = _submit(client)
    _submit(client, {**ENQUIRY, "email": "second@example.com"})
    assert _counts(client)["inbox_unresolved"] == 2

    client.patch(f"/api/v1/guest-contacts/{gc_id}/resolved", params={"resolved": True})
    assert _counts(client)["inbox_unresolved"] == 1

    client.patch(f"/api/v1/guest-contacts/{gc_id}/resolved", params={"resolved": False})
    assert _counts(client)["inbox_unresolved"] == 2


def test_quarantined_rows_are_not_owed_a_reply(client):
    """Nobody answers spam, so it must not inflate the outstanding count."""
    _submit(client, BOT)
    counts = _counts(client)
    assert (counts["spam"], counts["inbox_unresolved"]) == (1, 0)


def test_marking_a_reply_on_a_missing_row_is_a_404(client):
    missing = "aaaaaaaa-0000-0000-0000-aaaaaaaaaaaa"
    resp = client.patch(f"/api/v1/guest-contacts/{missing}/resolved", params={"resolved": True})
    assert resp.status_code == 404
