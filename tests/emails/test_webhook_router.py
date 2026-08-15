"""Tests for the SNS signing-cert host allow-list and signature verification gate.

Only the reject paths that don't require a real network fetch are exercised
here (host allow-list, missing/garbage signature) — these are exactly the
paths that stop a forged bounce/complaint payload from writing a bogus
EmailSuppression row.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import settings
from src.emails import webhook_router
from src.main import app


def test_signing_cert_url_allow_list_accepts_real_sns_host():
    assert webhook_router._SIGNING_CERT_URL_RE.match(
        "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc123.pem"
    )


def test_signing_cert_url_allow_list_rejects_spoofed_host():
    assert not webhook_router._SIGNING_CERT_URL_RE.match(
        "https://evil.example.com/sns.us-east-1.amazonaws.com/fake.pem"
    )


def test_signing_cert_url_allow_list_rejects_non_pem_path():
    assert not webhook_router._SIGNING_CERT_URL_RE.match("https://sns.us-east-1.amazonaws.com/not-a-cert")


def test_verify_sns_signature_rejects_disallowed_cert_host():
    message = {
        "Type": "Notification",
        "SigningCertURL": "https://attacker.example.com/cert.pem",
        "Signature": "irrelevant",
    }
    assert webhook_router._verify_sns_signature(message) is False


def test_verify_sns_signature_rejects_missing_signature_field():
    message = {
        "Type": "Notification",
        "SigningCertURL": "https://sns.us-east-1.amazonaws.com/SimpleNotificationService-abc123.pem",
        # No "Signature" key — cert fetch would be attempted then KeyError caught.
    }
    assert webhook_router._verify_sns_signature(message) is False


def test_webhook_rejects_validly_signed_message_from_wrong_topic(monkeypatch):
    """A genuine AWS signature from an attacker-owned topic must not be able to
    suppress contacts or confirm a subscription — the TopicArn pin blocks it."""
    monkeypatch.setattr(settings, "ses_sns_topic_arn", "arn:aws:sns:us-east-1:111:cmm-ses-bounce-complaint")
    # Signature verification passes (simulating a real AWS-signed payload), but
    # the topic is the attacker's own.
    monkeypatch.setattr(webhook_router, "_verify_sns_signature", lambda _m: True)
    forged = {
        "Type": "Notification",
        "TopicArn": "arn:aws:sns:us-east-1:999:attacker-topic",
        "Message": '{"eventType":"Bounce","bounce":{"bouncedRecipients":[{"emailAddress":"victim@example.com"}]}}',
    }
    with TestClient(app) as client:
        resp = client.post("/api/v1/emails/webhook", json=forged)
    assert resp.status_code == 401


def test_webhook_accepts_message_from_expected_topic(monkeypatch):
    """The matching-topic path is not rejected by the pin (subscription confirm
    is attempted; network call is stubbed so no real request goes out)."""
    topic = "arn:aws:sns:us-east-1:111:cmm-ses-bounce-complaint"
    monkeypatch.setattr(settings, "ses_sns_topic_arn", topic)
    monkeypatch.setattr(webhook_router, "_verify_sns_signature", lambda _m: True)
    monkeypatch.setattr(webhook_router.httpx, "get", lambda *a, **k: None)
    msg = {"Type": "SubscriptionConfirmation", "TopicArn": topic, "SubscribeURL": "https://sns/confirm"}
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/emails/webhook",
            json=msg,
            headers={"x-amz-sns-message-type": "SubscriptionConfirmation"},
        )
    assert resp.status_code == 200
