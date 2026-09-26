"""The Parents tab as an admin experiences it: filing, override, and precedence.

The classifier itself is covered in test_parent_detection.py. What matters here
is that the three tabs partition the table — every row appears in exactly one —
and that a parent enquiry is routed, never hidden: it is real mail from a real
family, it just does not belong in a queue meant for schools.

The ``client`` fixture lives in conftest.py.
"""

from __future__ import annotations

from src.guest_contacts.models import GuestContact

COUNSELLOR = {
    "first_name": "Patricia",
    "last_name": "Davico",
    "email": "patricia@example.com",
    "school_name": "St. Ignatius College Prep",
    "message": (
        "I direct college counselling here and would like to talk about bringing "
        "your financial aid sessions to our school this year."
    ),
}
PARENT = {
    "first_name": "Dana",
    "last_name": "Whitfield",
    "email": "dana@example.com",
    "school_name": "Campbell Hall",
    "message": "My son is a senior and we did not get the aid package we expected.",
}
BOT = {
    "first_name": "ussppyXbAPxyhvUi",
    "last_name": "BDJZjHHdCzIsbpyEIDpPMZsn",
    "email": "buni.q.in783@gmail.com",
    "school_name": "CUDKcLwIRfACLYGlbxLABui",
    "message": "7452959171",
}


def _post(client, body, ip="203.0.113.7"):
    return client.post("/api/v1/guest-contacts", json=body, headers={"x-forwarded-for": ip})


def _stored(client):
    db = client._session_local()
    try:
        return db.query(GuestContact).order_by(GuestContact.created_at).all()
    finally:
        db.close()


def _names(rows):
    return sorted(r["first_name"] for r in rows)


# ── Filing on the way in ─────────────────────────────────────────────

def test_a_parent_enquiry_is_filed_rather_than_quarantined(client):
    assert _post(client, PARENT).status_code == 201
    (row,) = _stored(client)
    assert (row.is_parent, row.parent_reason) == (True, "mentions_own_child")
    assert row.is_spam is False  # real mail, only shelved elsewhere


def test_the_public_response_gives_nothing_away(client):
    assert set(_post(client, PARENT).json()) == {"id", "created_at"}


# ── The three tabs partition the table ───────────────────────────────

def test_each_row_lands_in_exactly_one_tab(client):
    for body in (COUNSELLOR, PARENT, BOT):
        _post(client, body)

    inbox = client.get("/api/v1/guest-contacts", params={"parent": False}).json()
    parents = client.get("/api/v1/guest-contacts", params={"parent": True}).json()
    spam = client.get("/api/v1/guest-contacts", params={"spam": True}).json()

    assert _names(inbox) == ["Patricia"]
    assert _names(parents) == ["Dana"]
    assert _names(spam) == ["ussppyXbAPxyhvUi"]


def test_omitting_the_audience_returns_everything_that_is_not_spam(client):
    """The dashboard's recent-activity panel asks for exactly this and must not
    start dropping parent enquiries because a tab was added elsewhere."""
    for body in (COUNSELLOR, PARENT, BOT):
        _post(client, body)

    assert _names(client.get("/api/v1/guest-contacts").json()) == ["Dana", "Patricia"]


def test_a_quarantined_parent_stays_in_spam(client):
    """Spam wins: junk that happens to mention a daughter is still junk, and the
    Parents tab must not become a second place to find it."""
    _post(client, {**BOT, "message": "my daughter 7452959171"})
    (row,) = _stored(client)
    assert (row.is_spam, row.is_parent) == (True, True)

    assert client.get("/api/v1/guest-contacts", params={"parent": True}).json() == []
    assert len(client.get("/api/v1/guest-contacts", params={"spam": True}).json()) == 1


# ── Admin override ───────────────────────────────────────────────────

def test_admin_can_move_a_missed_parent_out_of_the_inbox(client):
    _post(client, COUNSELLOR)
    (row,) = _stored(client)

    resp = client.patch(f"/api/v1/guest-contacts/{row.id}/parent", params={"is_parent": True})
    assert resp.status_code == 200
    assert resp.json()["is_parent"] is True
    assert resp.json()["parent_reason"] == "marked_by_admin"

    assert client.get("/api/v1/guest-contacts", params={"parent": False}).json() == []
    assert len(client.get("/api/v1/guest-contacts", params={"parent": True}).json()) == 1


def test_admin_can_send_a_misfiled_row_back_to_the_inbox(client):
    _post(client, PARENT)
    (row,) = _stored(client)

    resp = client.patch(f"/api/v1/guest-contacts/{row.id}/parent", params={"is_parent": False})
    assert resp.status_code == 200
    # Recorded, not blanked — this is what the backfill checks before re-filing.
    assert resp.json()["parent_reason"] == "restored_by_admin"
    assert _names(client.get("/api/v1/guest-contacts", params={"parent": False}).json()) == ["Dana"]


def test_moving_a_row_between_audiences_does_not_touch_its_spam_verdict(client):
    _post(client, PARENT)
    (row,) = _stored(client)
    client.patch(f"/api/v1/guest-contacts/{row.id}/parent", params={"is_parent": False})
    assert _stored(client)[0].is_spam is False


def test_override_on_an_unknown_id_is_a_404(client):
    resp = client.patch(
        "/api/v1/guest-contacts/00000000-0000-0000-0000-000000000000/parent",
        params={"is_parent": True},
    )
    assert resp.status_code == 404


# ── Counts ───────────────────────────────────────────────────────────

def test_counts_track_each_tab_and_what_is_owed_a_reply(client):
    for body in (COUNSELLOR, PARENT, BOT):
        _post(client, body)
    parent_row = next(r for r in _stored(client) if r.first_name == "Dana")
    client.patch(f"/api/v1/guest-contacts/{parent_row.id}/resolved", params={"resolved": True})

    assert client.get("/api/v1/guest-contacts/counts").json() == {
        "inbox": 1,
        "parents": 1,
        "spam": 1,
        "inbox_unresolved": 1,
        "parents_unresolved": 0,
    }


def test_counts_on_an_empty_table_are_zero_not_null(client):
    assert client.get("/api/v1/guest-contacts/counts").json() == {
        "inbox": 0,
        "parents": 0,
        "spam": 0,
        "inbox_unresolved": 0,
        "parents_unresolved": 0,
    }
