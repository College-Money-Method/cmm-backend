"""The `recording.completed` webhook branch.

Zoom disables an endpoint that answers non-2xx repeatedly, so the contract this
file protects is: the handler answers 200 and hands the work to a background
task, whatever the payload turns out to contain.
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


def _payload(**object_fields) -> dict:
    return {"event": "recording.completed", "payload": {"object": object_fields}}


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
