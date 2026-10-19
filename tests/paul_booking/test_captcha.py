"""The booking form's Cloudflare Turnstile check: off without a secret, fail-closed with one."""

from __future__ import annotations

import httpx
import pytest

from src.config import settings
from src.integrations import turnstile
from src.paul_booking.models import PaulMeetingRequest
from tests.paul_booking.conftest import first_slots, post_request


@pytest.fixture
def cloudflare(monkeypatch):
    """Turnstile on, with Cloudflare's answer set per test via ``answer``."""
    monkeypatch.setattr(settings, "turnstile_secret_key", "test-secret")
    state = {"answer": {"success": True, "action": "paul_booking"}, "calls": []}

    def fake_post(url, data=None, **kw):
        state["calls"].append(data)
        answer = state["answer"]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(200, json=answer, request=httpx.Request("POST", url))

    monkeypatch.setattr(turnstile.httpx, "post", fake_post)
    return state


def _held(client) -> int:
    db = client._session_local()
    try:
        return db.query(PaulMeetingRequest).count()
    finally:
        db.close()


def test_without_a_secret_no_token_is_needed(booking_on):
    assert post_request(booking_on, first_slots(booking_on)[5]).status_code == 202


def test_cloudflare_test_key_answer_without_action_passes(booking_on, cloudflare):
    cloudflare["answer"] = {"success": True, "metadata": {"result_with_testing_key": True}}
    assert post_request(booking_on, first_slots(booking_on)[5], captcha_token="tok").status_code == 202


def test_passing_token_takes_the_hold(booking_on, cloudflare):
    resp = post_request(booking_on, first_slots(booking_on)[5], captcha_token="tok", ip="198.51.100.4")
    assert resp.status_code == 202
    (sent,) = cloudflare["calls"]
    assert sent == {"secret": "test-secret", "response": "tok", "remoteip": "198.51.100.4"}


@pytest.mark.parametrize("answer", [
    {"success": False, "error-codes": ["invalid-input-response"]},
    {"success": True, "action": "contact_form"},  # a token minted for another widget
    {"success": True, "action": ""},  # a widget rendered with no action
    httpx.ConnectError("down"),
])
def test_failed_check_is_400_and_holds_nothing(booking_on, cloudflare, answer):
    cloudflare["answer"] = answer
    assert post_request(booking_on, first_slots(booking_on)[5], captcha_token="tok").status_code == 400
    assert _held(booking_on) == 0


def test_missing_token_is_400_without_asking_cloudflare(booking_on, cloudflare):
    assert post_request(booking_on, first_slots(booking_on)[5]).status_code == 400
    assert cloudflare["calls"] == []
