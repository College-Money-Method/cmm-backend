"""The webhook branches that start the video pipeline.

Zoom disables an endpoint that answers non-2xx repeatedly, so the contract this
file protects is: the handler answers 200 and hands the work to a background
task, whatever the payload turns out to contain.

Two events start a job — `recording.completed` and, as a second chance at a
delivery Zoom admits it can drop, `recording.transcript_completed`. Both reach
the same intake, which is idempotent on the recording UUID.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import settings
from src.zoom import webhook_router

SECRET = "test-webhook-secret"
RECORDING_UUID = "aB3/cD4+eF5=="
ZOOM_WEBINAR_ID = "88812345678"


@pytest.fixture
def calls(monkeypatch):
    """Record intake invocations instead of touching the database."""
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(
        webhook_router,
        "intake_recording",
        lambda zoom_webinar_id, recording_uuid: recorded.append((zoom_webinar_id, recording_uuid)),
    )
    monkeypatch.setattr(settings, "zoom_webhook_secret_token", SECRET)
    return recorded


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(webhook_router.router)
    return TestClient(app)


def _post(client, payload: dict):
    """POST with a correctly signed body, as Zoom would."""
    body = json.dumps(payload)
    timestamp = "1700000000"
    signature = "v0=" + hmac.new(
        SECRET.encode(), f"v0:{timestamp}:{body}".encode(), hashlib.sha256
    ).hexdigest()
    return client.post(
        "/api/v1/zoom/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-zm-request-timestamp": timestamp,
            "x-zm-signature": signature,
        },
    )


def _payload(event: str = "recording.completed", **object_fields) -> dict:
    return {"event": event, "payload": {"object": object_fields}}


def test_recording_completed_queues_intake(client, calls):
    response = _post(client, _payload(uuid=RECORDING_UUID, id=ZOOM_WEBINAR_ID))

    assert response.status_code == 200
    assert calls == [(ZOOM_WEBINAR_ID, RECORDING_UUID)]


def test_download_token_is_never_persisted(client, calls):
    """No Zoom credential at rest: the task re-fetches a fresh URL over OAuth."""
    response = _post(
        client,
        {
            "event": "recording.completed",
            "download_token": "eyJzdXBlci1zZWNyZXQiOiJ0b2tlbiJ9",
            "payload": {"object": {"uuid": RECORDING_UUID, "id": ZOOM_WEBINAR_ID}},
        },
    )

    assert response.status_code == 200
    # Intake receives identifiers only — the token is not among them.
    assert calls == [(ZOOM_WEBINAR_ID, RECORDING_UUID)]


def test_transcript_completed_queues_the_same_intake(client, calls):
    """The second chance. Zoom drops `recording.completed` often enough that a
    recording reaching us only by its transcript event must still be published."""
    response = _post(
        client,
        _payload("recording.transcript_completed", uuid=RECORDING_UUID, id=ZOOM_WEBINAR_ID),
    )

    assert response.status_code == 200
    assert calls == [(ZOOM_WEBINAR_ID, RECORDING_UUID)]


def test_both_events_for_one_recording_both_reach_intake(client, calls):
    """The router does not try to be clever about which event came first.

    De-duplication belongs to intake, which holds the recording UUID and the
    job row; a second guard here could only disagree with it.
    """
    _post(client, _payload(uuid=RECORDING_UUID, id=ZOOM_WEBINAR_ID))
    _post(
        client,
        _payload("recording.transcript_completed", uuid=RECORDING_UUID, id=ZOOM_WEBINAR_ID),
    )

    assert calls == [(ZOOM_WEBINAR_ID, RECORDING_UUID)] * 2


def test_an_unhandled_recording_event_is_ignored(client, calls):
    """Only the two events that mean "ready to publish" start a job — a trashed
    or deleted recording arriving here must not queue one."""
    response = _post(client, _payload("recording.trashed", uuid=RECORDING_UUID, id=ZOOM_WEBINAR_ID))

    assert response.status_code == 200
    assert calls == []


def test_payload_without_identifiers_still_answers_200(client, calls):
    response = _post(client, _payload())

    assert response.status_code == 200
    assert calls == []


def test_bad_signature_is_rejected(client, calls):
    response = client.post(
        "/api/v1/zoom/webhook",
        content=json.dumps(_payload(uuid=RECORDING_UUID, id=ZOOM_WEBINAR_ID)),
        headers={
            "content-type": "application/json",
            "x-zm-request-timestamp": "1700000000",
            "x-zm-signature": "v0=not-the-real-signature",
        },
    )

    assert response.status_code == 401
    assert calls == []
