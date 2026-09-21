"""The admin Q&A endpoints.

Two things the screen depends on and cannot check for itself: the answer shown
is the one the precedence rule picks, not whichever column happened to be
populated; and an edit lands in the override columns, which the next Zoom pull
and the next extraction both leave alone.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.auth.deps import get_current_user
from src.auth.schemas import CurrentUser
from src.db.client import get_supabase
from src.db.deps import get_db
from src.main import app
from src.video_pipeline.models import WebinarVideoJob
from src.video_pipeline.states import JobState
from src.workshops import qa_router
from src.workshops.models import Webinar, Workshop
from src.workshops.qa_models import WebinarQaAnswerExtraction, WebinarQaQuestion

ADMIN_USER_ID = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
WORKSHOP_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
WEBINAR_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
OTHER_WEBINAR_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
JOB_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")

# Ids chosen so the fixtures can be addressed by name in an assertion.
TYPED_ID = uuid.UUID("aaaa0001-0000-0000-0000-000000000000")
LIVE_ID = uuid.UUID("aaaa0002-0000-0000-0000-000000000000")
OVERRIDDEN_ID = uuid.UUID("aaaa0003-0000-0000-0000-000000000000")
OTHER_ID = uuid.UUID("aaaa0005-0000-0000-0000-000000000000")
NOISE_ID = uuid.UUID("aaaa0006-0000-0000-0000-000000000000")
UNLABELLED_ID = uuid.UUID("aaaa0007-0000-0000-0000-000000000000")

START = datetime(2026, 9, 15, 23, 22, tzinfo=timezone.utc)
BASE = "/api/v1/admin/webinar-qa"


def _question(question_id, webinar_id, text, offset_minutes, **kwargs):
    fields = {
        "answer_source": "unanswered",
        "classification": "question",
        **kwargs,
    }
    return WebinarQaQuestion(
        id=question_id,
        webinar_id=webinar_id,
        zoom_question_id=str(question_id),
        question_text=text,
        asked_at=START + timedelta(minutes=offset_minutes),
        **fields,
    )


@pytest.fixture
def seeded(qa_sessionmaker):
    """One webinar with a typed answer, a spoken one, an override and a hidden row."""
    seed = qa_sessionmaker()
    seed.add(Workshop(id=WORKSHOP_ID, name="Paying for College"))
    seed.add(
        Webinar(
            id=WEBINAR_ID,
            workshop_id=WORKSHOP_ID,
            webinar_name="Paying for College — Sept",
            zoom_webinar_id="83822890565",
        )
    )
    seed.add(
        Webinar(
            id=OTHER_WEBINAR_ID,
            workshop_id=WORKSHOP_ID,
            webinar_name="Paying for College — Oct",
            zoom_webinar_id="99999999999",
        )
    )
    seed.add(
        WebinarVideoJob(
            id=JOB_ID,
            webinar_id=WEBINAR_ID,
            zoom_recording_uuid="rec-uuid-1",
            state=JobState.PUBLISHED.value,
            frames_prefix="webinars/abc/frames/",
        )
    )
    seed.flush()

    seed.add_all(
        [
            _question(
                TYPED_ID,
                WEBINAR_ID,
                "Does the CSS Profile count home equity?",
                1,
                answer_source="typed",
                typed_answer_text="It depends on the school.",
                asker_name="Dana Reed",
            ),
            _question(LIVE_ID, WEBINAR_ID, "What about a second home?", 2, answer_source="live"),
            _question(
                OVERRIDDEN_ID,
                WEBINAR_ID,
                "How late can I file?",
                3,
                answer_source="typed",
                typed_answer_text="Zoom's version",
                answer_text_override="The admin's corrected version",
            ),
            _question(OTHER_ID, OTHER_WEBINAR_ID, "A question from October", 5),
            # Noise the model labelled but no admin has touched — the case the
            # default view exists to drop.
            _question(
                NOISE_ID,
                OTHER_WEBINAR_ID,
                "Thank you so much, this is excellent!",
                6,
                classification="thanks",
            ),
            # The model never reached a verdict on this one. It is a real
            # question and must not be swept up with the noise.
            _question(
                UNLABELLED_ID,
                OTHER_WEBINAR_ID,
                "Is there a replay?",
                7,
                classification=None,
            ),
        ]
    )
    seed.flush()
    seed.add(
        WebinarQaAnswerExtraction(
            id=uuid.uuid4(),
            question_id=LIVE_ID,
            video_job_id=JOB_ID,
            status="extracted",
            answer_text="Only the school's own aid is affected.",
            answered_by="Paul Martin",
            transcript_start_seconds=676,
            transcript_end_seconds=728,
            confidence=Decimal("0.92"),
            prompt_version="v1",
        )
    )
    seed.commit()
    seed.close()
    return qa_sessionmaker


@pytest.fixture
def client(seeded):
    def override_get_db():
        db = seeded()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=ADMIN_USER_ID, email="admin@collegemoneymethod.com", role="super_admin"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def viewer_client(seeded):
    def override_get_db():
        db = seeded()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_supabase] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=uuid.uuid4(), email="viewer@collegemoneymethod.com", role="viewer"
    )
    yield TestClient(app)
    app.dependency_overrides.clear()


def _ids(resp) -> list[str]:
    return [item["id"] for item in resp.json()["items"]]


def _by_id(resp) -> dict:
    return {item["id"]: item for item in resp.json()["items"]}


# ── access ───────────────────────────────────────────────────────────────────


def test_a_non_admin_cannot_reach_any_of_it(viewer_client):
    # Q&A carries attendee names, emails and family circumstances, so even the
    # read-only viewer role is refused — every route is super_admin only.
    assert viewer_client.get(f"{BASE}/questions").status_code == 403
    assert viewer_client.get(f"{BASE}/questions/{TYPED_ID}").status_code == 403
    assert (
        viewer_client.patch(
            f"{BASE}/questions/{TYPED_ID}", json={"classification_override": "comment"}
        ).status_code
        == 403
    )
    assert viewer_client.post(f"{BASE}/webinars/{WEBINAR_ID}/resync").status_code == 403
    assert (
        viewer_client.post(f"{BASE}/webinars/{WEBINAR_ID}/re-extract").status_code == 403
    )


# ── listing ──────────────────────────────────────────────────────────────────


def test_classified_noise_is_out_of_the_list_by_default(client):
    listed = _ids(client.get(f"{BASE}/questions"))
    assert str(NOISE_ID) not in listed

    # Labelled, never deleted: both ways back to it still work.
    assert str(NOISE_ID) in _ids(client.get(f"{BASE}/questions", params={"include_noise": True}))
    assert str(NOISE_ID) in _ids(
        client.get(f"{BASE}/questions", params={"classification": "thanks"})
    )


def test_an_unclassified_question_is_not_treated_as_noise(client):
    # Labelling reaches Bedrock and can fail. Reading a missing label as noise
    # would drop real questions on exactly the runs that went wrong.
    assert str(UNLABELLED_ID) in _ids(client.get(f"{BASE}/questions"))


def test_the_answered_filter_covers_every_layer_an_answer_can_come_from(client):
    answered = _ids(client.get(f"{BASE}/questions", params={"answered": True}))
    # A panelist's typed reply, a transcript extraction, and an admin's
    # correction — all three count, which is the whole point of asking the
    # resolver's SQL twin rather than reading one column.
    assert set(answered) == {str(TYPED_ID), str(LIVE_ID), str(OVERRIDDEN_ID)}

    unanswered = _ids(client.get(f"{BASE}/questions", params={"answered": False}))
    assert set(unanswered) == {str(OTHER_ID), str(UNLABELLED_ID)}


def test_answered_is_not_the_same_question_as_answer_source(client):
    # `answer_source` records what Zoom said happened in the session; `answered`
    # asks whether any text exists now. A live-answered question has none until
    # the extraction recovers it, so the two disagree by design.
    live = _ids(client.get(f"{BASE}/questions", params={"answer_source": "live"}))
    assert live == [str(LIVE_ID)]
    assert str(LIVE_ID) in _ids(client.get(f"{BASE}/questions", params={"answered": True}))


def test_the_list_is_in_the_order_the_questions_were_asked(client):
    resp = client.get(f"{BASE}/questions", params={"webinar_id": str(WEBINAR_ID)})
    assert _ids(resp) == [str(TYPED_ID), str(LIVE_ID), str(OVERRIDDEN_ID)]


def test_the_webinar_filter_excludes_other_sessions(client):
    resp = client.get(f"{BASE}/questions", params={"webinar_id": str(WEBINAR_ID)})
    assert str(OTHER_ID) not in _ids(resp)
    assert resp.json()["total"] == 3


def test_a_row_carries_the_session_it_came_from(client):
    row = _by_id(client.get(f"{BASE}/questions"))[str(TYPED_ID)]
    assert row["webinar_name"] == "Paying for College — Sept"
    assert row["workshop_name"] == "Paying for College"


def test_search_matches_the_question_text_and_the_asker(client):
    assert _ids(client.get(f"{BASE}/questions", params={"search": "home equity"})) == [
        str(TYPED_ID)
    ]
    assert _ids(client.get(f"{BASE}/questions", params={"search": "dana"})) == [str(TYPED_ID)]


def test_an_unknown_classification_is_refused(client):
    resp = client.get(f"{BASE}/questions", params={"classification": "kwestion"})
    assert resp.status_code == 422


def test_the_classification_filter_follows_an_admins_relabel(client):
    client.patch(
        f"{BASE}/questions/{TYPED_ID}", json={"classification_override": "comment"}
    )

    as_question = _ids(client.get(f"{BASE}/questions", params={"classification": "question"}))
    as_comment = _ids(client.get(f"{BASE}/questions", params={"classification": "comment"}))

    # Found under the label the admin gave it, not the model's.
    assert str(TYPED_ID) not in as_question
    assert str(TYPED_ID) in as_comment


def test_paging_reports_the_full_total(client):
    resp = client.get(f"{BASE}/questions", params={"limit": 1, "offset": 1})
    body = resp.json()
    assert len(body["items"]) == 1
    assert (body["total"], body["limit"], body["offset"]) == (5, 1, 1)


# ── the resolved answer ──────────────────────────────────────────────────────


def test_a_typed_answer_is_shown_as_zoom_recorded_it(client):
    row = _by_id(client.get(f"{BASE}/questions"))[str(TYPED_ID)]
    assert row["resolved_answer"] == "It depends on the school."
    assert row["resolved_answer_source"] == "typed"


def test_a_spoken_answer_comes_with_the_speaker_and_its_place_in_the_replay(client):
    row = _by_id(client.get(f"{BASE}/questions"))[str(LIVE_ID)]
    assert row["resolved_answer"] == "Only the school's own aid is affected."
    assert row["resolved_answer_source"] == "extracted"
    assert row["resolved_answered_by"] == "Paul Martin"
    # Seconds on the published replay's clock, so the UI can deep-link.
    assert (row["resolved_start_seconds"], row["resolved_end_seconds"]) == (676, 728)
    # The list shows how sure the model was, so the confidence has to travel
    # with the answer — the screen cannot pick the best extraction itself.
    assert row["resolved_confidence"] == pytest.approx(0.92)
    assert row["extraction_count"] == 1


def test_an_answer_zoom_recorded_itself_carries_no_confidence(client):
    row = _by_id(client.get(f"{BASE}/questions"))[str(TYPED_ID)]
    assert row["resolved_confidence"] is None


def test_an_admins_correction_wins_over_zooms_own_text(client):
    row = _by_id(client.get(f"{BASE}/questions"))[str(OVERRIDDEN_ID)]
    assert row["resolved_answer"] == "The admin's corrected version"
    assert row["resolved_answer_source"] == "override"
    assert row["has_override"] is True


def test_a_question_nobody_answered_says_so(client):
    row = _by_id(client.get(f"{BASE}/questions", params={"webinar_id": str(OTHER_WEBINAR_ID)}))[
        str(OTHER_ID)
    ]
    assert (row["resolved_answer"], row["resolved_answer_source"]) == (None, "unanswered")


def test_the_detail_view_keeps_the_ingested_fact_beside_the_correction(client):
    body = client.get(f"{BASE}/questions/{OVERRIDDEN_ID}").json()
    # The correction can always be compared against what actually came in.
    assert body["typed_answer_text"] == "Zoom's version"
    assert body["answer_text_override"] == "The admin's corrected version"


def test_the_detail_view_carries_every_verdict_ever_run(client):
    body = client.get(f"{BASE}/questions/{LIVE_ID}").json()
    assert [e["status"] for e in body["extractions"]] == ["extracted"]
    assert body["extractions"][0]["prompt_version"] == "v1"


def test_an_unknown_question_is_a_404(client):
    assert client.get(f"{BASE}/questions/{uuid.uuid4()}").status_code == 404


# ── editing ──────────────────────────────────────────────────────────────────


def test_an_edit_is_stamped_with_who_made_it(client, seeded):
    resp = client.patch(
        f"{BASE}/questions/{LIVE_ID}", json={"answer_text_override": "Said better"}
    )
    assert resp.status_code == 200
    assert resp.json()["resolved_answer_source"] == "override"

    db = seeded()
    row = db.get(WebinarQaQuestion, LIVE_ID)
    assert row.answer_text_override == "Said better"
    assert row.edited_by_user_id == ADMIN_USER_ID
    assert row.edited_at is not None
    db.close()


def test_an_edit_leaves_the_model_verdict_it_overrides_untouched(client, seeded):
    client.patch(f"{BASE}/questions/{LIVE_ID}", json={"answer_text_override": "Said better"})

    db = seeded()
    extraction = db.scalars(
        select(WebinarQaAnswerExtraction).where(
            WebinarQaAnswerExtraction.question_id == LIVE_ID
        )
    ).one()
    assert extraction.answer_text == "Only the school's own aid is affected."
    db.close()


def test_an_empty_override_takes_the_correction_back_off(client):
    resp = client.patch(f"{BASE}/questions/{OVERRIDDEN_ID}", json={"answer_text_override": ""})
    body = resp.json()
    # Cleared, not stored blank — Zoom's own answer surfaces again.
    assert body["answer_text_override"] is None
    assert body["resolved_answer"] == "Zoom's version"
    assert body["resolved_answer_source"] == "typed"


def test_an_unknown_label_cannot_be_written(client):
    resp = client.patch(
        f"{BASE}/questions/{TYPED_ID}", json={"classification_override": "kwestion"}
    )
    assert resp.status_code == 422


def test_an_edit_that_names_no_field_changes_nothing(client, seeded):
    assert client.patch(f"{BASE}/questions/{TYPED_ID}", json={}).status_code == 200

    db = seeded()
    assert db.get(WebinarQaQuestion, TYPED_ID).edited_at is None
    db.close()


# ── the two levers ───────────────────────────────────────────────────────────


def test_a_resync_reports_a_report_zoom_has_not_produced_as_a_wait(client, monkeypatch):
    monkeypatch.setattr(qa_router, "sync_webinar_qa", lambda zoom_id, db: False)

    body = client.post(f"{BASE}/webinars/{WEBINAR_ID}/resync").json()
    # A wait, not a failure: the same call works later.
    assert body["ok"] is False
    assert body["question_count"] == 3


def test_a_resync_of_a_webinar_with_no_zoom_id_is_refused(client, seeded, monkeypatch):
    monkeypatch.setattr(qa_router, "sync_webinar_qa", lambda zoom_id, db: True)
    db = seeded()
    db.get(Webinar, OTHER_WEBINAR_ID).zoom_webinar_id = None
    db.commit()
    db.close()

    assert client.post(f"{BASE}/webinars/{OTHER_WEBINAR_ID}/resync").status_code == 422


def test_a_re_extraction_runs_over_the_webinars_questions(client, monkeypatch):
    monkeypatch.setattr(qa_router, "extract_answers", lambda db, webinar_id: 3)

    body = client.post(f"{BASE}/webinars/{WEBINAR_ID}/re-extract").json()
    assert (body["ok"], body["question_count"]) == (True, 3)


def test_a_re_extraction_with_nothing_to_do_says_so(client, monkeypatch):
    monkeypatch.setattr(qa_router, "extract_answers", lambda db, webinar_id: 0)

    body = client.post(f"{BASE}/webinars/{WEBINAR_ID}/re-extract").json()
    assert body["ok"] is False
    assert "No live-answered questions" in body["detail"]


def test_an_unknown_webinar_is_a_404_on_both_levers(client):
    missing = uuid.uuid4()
    assert client.post(f"{BASE}/webinars/{missing}/resync").status_code == 404
    assert client.post(f"{BASE}/webinars/{missing}/re-extract").status_code == 404
