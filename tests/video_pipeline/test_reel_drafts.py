"""Reel drafts: the first cut, refining it in conversation or by hand, then rendering it.

The model is replaced by stubs that return real ``Selection`` objects, so the
rules around it — one turn at a time, edits recorded for the next turn, one
render per job, a lost turn put back — are tested against real rows. The
background turn runs inside ``TestClient`` once the response is sent.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from src.integrations import zoom
from src.video_pipeline import (
    archive_original,
    reel_draft,
    reel_draft_task,
    reel_sources,
    task_dispatch,
)
from src.video_pipeline.reel_models import (
    DRAFT,
    DRAFTING,
    FAILED,
    PENDING,
    RENDERING,
    WebinarVideoReel,
)
from src.video_pipeline.reel_service import ReelConflict
from src.video_pipeline.reel_sources import ReelInputs
from src.video_pipeline.trailer_select import Selection, validate
from src.video_pipeline.transcript import Cue
from tests.video_pipeline.test_reels import (  # noqa: F401 - fixtures
    BASE,
    _reel,
    archive,
    client,
    launched,
    published_job,
    zoom_recording,
)

CUES = [Cue(start=i * 6.0, end=(i + 1) * 6.0 - 0.5, text=f"Line {i}.") for i in range(100)]


def _cut(*ranges: tuple[int, int], title: str = "Hook") -> Selection:
    return validate({"hook_title": title, "segments": [
        {"first_cue": a, "last_cue": b, "why": "", "flags": []} for a, b in ranges]}, CUES)


FIRST_CUT = _cut((10, 12), (20, 22), (30, 32))


@pytest.fixture
def model(sessionmaker_factory, monkeypatch, archive):
    """Stub the turn's inputs and the model; records what each turn was asked."""
    archive.add(archive_original.CAMERA_FILENAME)
    monkeypatch.setattr(reel_draft_task, "get_session_factory", lambda: sessionmaker_factory)
    monkeypatch.setattr(reel_sources, "load_inputs", lambda job: ReelInputs(
        title="T", cues=CUES, chapters=[], trim_offset=12.5))
    seen: dict = {"select": [], "revise": [], "answer": FIRST_CUT, "reply": "Swapped it."}

    def select(cues, chapters, title, **kw):
        seen["select"].append(kw)
        if isinstance(seen["answer"], Exception):
            raise seen["answer"]
        return seen["answer"]

    def revise(cues, chapters, title, **kw):
        seen["revise"].append(kw)
        if isinstance(seen["answer"], Exception):
            raise seen["answer"]
        return seen["answer"], seen["reply"]

    monkeypatch.setattr(reel_draft_task, "select_segments", select)
    monkeypatch.setattr(reel_draft_task, "revise_segments", revise)
    return seen


def _draft(db, job, selection: Selection | None = FIRST_CUT, **fields) -> WebinarVideoReel:
    return _reel(db, job, state=DRAFT, orientation=None,
                 selection=selection.as_dict() if selection else None,
                 hook_title=selection.hook_title if selection else None, **fields)


def _fresh(db, reel_id) -> WebinarVideoReel:
    db.expire_all()
    return db.get(WebinarVideoReel, uuid.UUID(str(reel_id)))


# --- the first cut -------------------------------------------------------------------------

def test_creating_a_reel_drafts_a_first_cut(client, db, published_job, model):
    response = client.post(f"{BASE}/jobs/{published_job.id}/reels",
                           json={"prompt": " Focus on merit aid "})

    assert response.status_code == 201
    body = response.json()
    assert body["state"] == DRAFTING and body["orientation"] is None
    assert [m["role"] for m in body["messages"]] == ["admin"]
    assert model["select"] == [{"focus": "Focus on merit aid"}]

    listed = client.get(f"{BASE}/jobs/{published_job.id}/reels").json()["items"][0]
    assert listed["state"] == DRAFT and listed["hook_title"] == "Hook"
    assert [m["role"] for m in listed["messages"]] == ["admin", "assistant"]
    assert listed["messages"][1]["text"].startswith("Here's a first cut: 3 clips")
    segment = listed["selection"]["segments"][0]
    assert segment["lines"][0] == {"start": 60.0, "end": 65.5, "text": "Line 10."}
    assert segment["duration"] == pytest.approx(segment["end"] - segment["start"])
    assert listed["problems"] == []


def test_a_draft_needs_no_direction(client, published_job, model):
    body = client.post(f"{BASE}/jobs/{published_job.id}/reels", json={}).json()
    assert body["messages"] == [] and model["select"] == [{"focus": None}]


def test_a_draft_does_not_hold_the_render_slot(client, db, published_job, model, launched):
    _reel(db, published_job, state=RENDERING)
    response = client.post(f"{BASE}/jobs/{published_job.id}/reels", json={})
    assert response.status_code == 201 and launched == []


def test_a_blocked_job_refuses_a_draft(client, published_job, archive, zoom_recording):
    zoom_recording["payload"] = zoom.ZoomApiError("gone")
    response = client.post(f"{BASE}/jobs/{published_job.id}/reels", json={})
    assert response.status_code == 409
    assert "Zoom no longer has" in response.json()["detail"]


def test_a_long_direction_is_rejected(client, published_job):
    response = client.post(f"{BASE}/jobs/{published_job.id}/reels", json={"prompt": "x" * 501})
    assert response.status_code == 422


def test_a_failed_first_cut_leaves_the_draft_to_retry(client, db, published_job, model):
    model["answer"] = RuntimeError("Bedrock said no")
    reel_id = client.post(f"{BASE}/jobs/{published_job.id}/reels",
                          json={"prompt": "Merit aid"}).json()["id"]

    failed = _fresh(db, reel_id)
    assert failed.state == DRAFT and failed.selection is None
    assert "Bedrock said no" in failed.error
    assert [m["role"] for m in failed.messages] == ["admin"]

    # Asking again retries the first cut, with everything asked for so far.
    model["answer"] = FIRST_CUT
    client.post(f"{BASE}/reels/{reel_id}/messages", json={"text": "Keep it upbeat"})
    assert model["select"][-1] == {"focus": "Merit aid\nKeep it upbeat"}
    assert _fresh(db, reel_id).selection is not None


# --- refining in conversation -------------------------------------------------------------

def test_a_message_revises_the_cut_with_the_conversation(client, db, published_job, model):
    reel = _draft(db, published_job, prompt="Merit aid",
                  messages=[reel_draft.message("admin", "Merit aid"),
                            reel_draft.message("assistant", "Here's a first cut")])
    model["answer"] = _cut((10, 12), (40, 42), (30, 32), title="Hook two")

    body = client.post(f"{BASE}/reels/{reel.id}/messages",
                       json={"text": "Swap clip 2"}).json()

    assert body["state"] == DRAFTING
    asked = model["revise"][0]
    assert asked["request"] == "Swap clip 2" and asked["focus"] == "Merit aid"
    assert [m["role"] for m in asked["conversation"]] == ["admin", "assistant"]
    assert asked["current"].segments[1].first_cue == 20
    done = _fresh(db, reel.id)
    assert done.state == DRAFT and done.hook_title == "Hook two"
    assert done.selection["segments"][1]["first_cue"] == 40
    assert done.messages[-1]["role"] == "assistant"
    assert done.messages[-1]["text"] == "Swapped it."


def test_a_failed_revision_keeps_the_cut(client, db, published_job, model):
    reel = _draft(db, published_job)
    model["answer"] = RuntimeError("no usable reel")
    client.post(f"{BASE}/reels/{reel.id}/messages", json={"text": "One clip only"})

    kept = _fresh(db, reel.id)
    assert kept.state == DRAFT and kept.selection == FIRST_CUT.as_dict()
    assert "no usable reel" in kept.error


def test_one_turn_at_a_time(client, db, published_job):
    reel = _reel(db, published_job, state=DRAFTING, orientation=None)
    response = client.post(f"{BASE}/reels/{reel.id}/messages", json={"text": "More"})
    assert response.status_code == 409
    assert response.json()["detail"] == reel_draft.BUSY


def test_a_rendered_reel_takes_no_messages(client, db, published_job):
    reel = _reel(db, published_job, state=RENDERING)
    response = client.post(f"{BASE}/reels/{reel.id}/messages", json={"text": "More"})
    assert response.status_code == 409
    assert response.json()["detail"] == reel_draft.NOT_A_DRAFT


def test_a_lost_turn_is_put_back_by_the_list(client, db, published_job):
    reel = _reel(db, published_job, state=DRAFTING, orientation=None)
    reel.updated_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    db.commit()

    item = client.get(f"{BASE}/jobs/{published_job.id}/reels").json()["items"][0]

    assert item["state"] == DRAFT and "send it again" in item["error"]


def test_a_turn_for_a_discarded_draft_writes_nothing(db, published_job, model):
    reel = _reel(db, published_job, state=DRAFTING, orientation=None)
    reel_id = reel.id
    db.delete(reel)
    db.commit()

    reel_draft_task.run_turn(reel_id)

    assert model["select"] == [] and _fresh(db, reel_id) is None


def test_a_turn_overtaken_while_it_ran_writes_nothing(db, published_job, model,
                                                     sessionmaker_factory, monkeypatch):
    """The list gave up on the turn and the admin asked again before it came back."""
    reel = _reel(db, published_job, state=DRAFTING, orientation=None)
    reel_id = reel.id

    def overtaken(*args, **kw):
        other = sessionmaker_factory()
        other.execute(update(WebinarVideoReel).where(WebinarVideoReel.id == reel_id).values(
            updated_at=datetime.now(timezone.utc) + timedelta(minutes=1),
            messages=[{"role": "admin", "text": "Newer ask", "at": "2026-01-01T00:00:00"}]))
        other.commit()
        other.close()
        return FIRST_CUT

    monkeypatch.setattr(reel_draft_task, "select_segments", overtaken)
    reel_draft_task.run_turn(reel_id)

    fresh = _fresh(db, reel_id)
    assert fresh.state == DRAFTING and fresh.selection is None
    assert [m["text"] for m in fresh.messages] == ["Newer ask"]


# --- editing by hand -----------------------------------------------------------------------

def test_reordering_and_removing_clips_is_recorded_for_the_next_turn(client, db,
                                                                     published_job):
    reel = _draft(db, published_job)

    body = client.patch(f"{BASE}/reels/{reel.id}/draft",
                        json={"segment_order": [2, 0], "hook_title": " New hook "}).json()

    assert body["state"] == DRAFT
    assert [s["first_cue"] for s in body["selection"]["segments"]] == [30, 10]
    assert body["selection"]["hook_title"] == "New hook" and body["hook_title"] == "New hook"
    note = body["messages"][-1]
    assert note["role"] == "edit"
    assert note["text"] == ('Removed clip 2 ("Line 20."); reordered the clips '
                            'to 3, 1 (by their old numbers); changed the hook title to '
                            '"New hook"')
    assert len(body["problems"]) == 2
    assert body["duration_seconds"] == pytest.approx(body["selection"]["total_seconds"])


def test_a_blank_hook_title_is_refused(client, db, published_job):
    reel = _draft(db, published_job)
    response = client.patch(f"{BASE}/reels/{reel.id}/draft", json={"hook_title": "   "})
    assert response.status_code == 422


def test_an_edit_that_changes_nothing_adds_no_note(client, db, published_job):
    reel = _draft(db, published_job)
    body = client.patch(f"{BASE}/reels/{reel.id}/draft",
                        json={"segment_order": [0, 1, 2], "hook_title": "Hook"}).json()
    assert body["messages"] == []


def test_a_bad_clip_order_is_rejected(client, db, published_job):
    reel = _draft(db, published_job)
    url = f"{BASE}/reels/{reel.id}/draft"
    assert client.patch(url, json={"segment_order": [0, 0]}).status_code == 422
    assert client.patch(url, json={"segment_order": [3]}).status_code == 422
    assert client.patch(url, json={"segment_order": []}).status_code == 422


def test_a_draft_without_clips_cannot_be_edited(client, db, published_job):
    reel = _draft(db, published_job, selection=None)
    response = client.patch(f"{BASE}/reels/{reel.id}/draft", json={"hook_title": "Hi"})
    assert response.status_code == 409


# --- rendering and discarding -------------------------------------------------------------

def test_rendering_a_draft_launches_its_task(client, db, published_job, archive, launched):
    archive.add(archive_original.CAMERA_FILENAME)
    reel = _draft(db, published_job)

    response = client.post(f"{BASE}/reels/{reel.id}/render", json={"orientation": "portrait"})

    assert response.status_code == 200
    body = response.json()
    assert body["state"] == RENDERING and body["orientation"] == "portrait"
    assert launched == [str(reel.id)]
    assert client.post(f"{BASE}/reels/{reel.id}/render",
                       json={"orientation": "portrait"}).status_code == 409


def test_without_ecs_a_rendered_draft_waits_pending(client, db, published_job, archive,
                                                    monkeypatch):
    archive.add(archive_original.CAMERA_FILENAME)
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: False)
    reel = _draft(db, published_job)
    body = client.post(f"{BASE}/reels/{reel.id}/render", json={"orientation": "landscape"}).json()
    assert body["state"] == PENDING


def test_a_launch_ecs_refuses_fails_the_reel(client, db, published_job, archive, monkeypatch):
    archive.add(archive_original.CAMERA_FILENAME)
    monkeypatch.setattr(task_dispatch, "is_configured", lambda: True)

    def refuse(_reel_id):
        raise RuntimeError("capacity unavailable")

    monkeypatch.setattr(task_dispatch, "_run_reel_task", refuse)
    reel = _draft(db, published_job)
    body = client.post(f"{BASE}/reels/{reel.id}/render", json={"orientation": "landscape"}).json()
    assert body["state"] == FAILED and "capacity unavailable" in body["error"]


def test_one_reel_renders_at_a_time(client, db, published_job, archive, launched):
    archive.add(archive_original.CAMERA_FILENAME)
    _reel(db, published_job, state=RENDERING)
    reel = _draft(db, published_job)
    response = client.post(f"{BASE}/reels/{reel.id}/render", json={"orientation": "portrait"})
    assert response.status_code == 409 and launched == []


def test_a_render_that_loses_the_race_is_a_conflict(client, db, published_job, archive,
                                                     launched, monkeypatch):
    archive.add(archive_original.CAMERA_FILENAME)
    _reel(db, published_job, state=RENDERING)
    # As if the other request committed between this one's check and update.
    monkeypatch.setattr(reel_draft, "list_reels", lambda _db, _job: [])
    reel = _draft(db, published_job)

    response = client.post(f"{BASE}/reels/{reel.id}/render", json={"orientation": "portrait"})

    assert response.status_code == 409 and launched == []
    assert _fresh(db, reel.id).state == DRAFT


def test_a_draft_needs_a_clip_and_an_orientation_to_render(client, db, published_job,
                                                            archive, launched):
    archive.add(archive_original.CAMERA_FILENAME)
    empty = _draft(db, published_job, selection=Selection(hook_title="H", segments=[]))
    url = f"{BASE}/reels/{empty.id}/render"
    assert client.post(url, json={"orientation": "square"}).status_code == 422
    assert client.post(url, json={"orientation": "portrait"}).status_code == 409
    assert launched == []


def test_a_draft_can_be_discarded_but_a_rendered_reel_cannot(client, db, published_job):
    draft = _draft(db, published_job)
    rendered = _reel(db, published_job, state=FAILED)

    assert client.delete(f"{BASE}/reels/{draft.id}").status_code == 204
    assert client.delete(f"{BASE}/reels/{rendered.id}").status_code == 409
    assert client.delete(f"{BASE}/reels/{draft.id}").status_code == 404



def test_discarding_refuses_a_reel_sent_to_render_since_it_was_read(db, published_job):
    reel = _draft(db, published_job)
    # Another request starts the render after this one read the row as a draft.
    db.execute(update(WebinarVideoReel).where(WebinarVideoReel.id == reel.id)
               .values(state=PENDING).execution_options(synchronize_session=False))
    db.commit()
    reel.state = DRAFT  # what this request still believes

    with pytest.raises(ReelConflict):
        reel_draft.discard(db, reel)
    assert _fresh(db, reel.id).state == PENDING
