"""Access token: audience, expiry, tamper and rotation behaviour."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

from src.colleges.access_password import hash_password
from src.colleges.access_token import (
    AUDIENCE,
    issue_access_token,
    password_version,
    verify_access_token,
)
from src.schools.src_session import signing_secret

STORED = hash_password("fake-pass-1234")


def _forge(**overrides) -> str:
    claims = {
        "sub": AUDIENCE, "aud": AUDIENCE, "pwv": password_version(STORED),
        "exp": datetime.now(UTC) + timedelta(hours=1), **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, signing_secret(), algorithm="HS256")


def test_issued_token_verifies_and_lasts_seven_days():
    token, expires_at = issue_access_token(STORED)
    assert verify_access_token(token, STORED)
    delta = expires_at - datetime.now(UTC)
    assert timedelta(days=6, hours=23) < delta <= timedelta(days=7)


def test_rotation_voids_token():
    token, _ = issue_access_token(STORED)
    assert not verify_access_token(token, hash_password("brand-new-pass"))


def test_cleared_password_voids_token():
    token, _ = issue_access_token(STORED)
    assert not verify_access_token(token, None)


def test_expired_token_rejected():
    assert not verify_access_token(_forge(exp=datetime.now(UTC) - timedelta(seconds=5)), STORED)


def test_tampered_token_rejected():
    token, _ = issue_access_token(STORED)
    head, payload, sig = token.split(".")
    assert not verify_access_token(f"{head}.{payload}.{sig[:-2]}xx", STORED)
    assert not verify_access_token("not-a-jwt", STORED)


def test_wrong_secret_rejected():
    token = jwt.encode({"sub": AUDIENCE, "aud": AUDIENCE, "pwv": password_version(STORED),
                        "exp": datetime.now(UTC) + timedelta(hours=1)}, "other-secret-xxxxxxxxxxxxxxxxxxxxxx", algorithm="HS256")
    assert not verify_access_token(token, STORED)


def test_missing_or_wrong_audience_rejected():
    assert not verify_access_token(_forge(aud=None), STORED)  # e.g. an SRC school token
    assert not verify_access_token(_forge(aud="someone-else"), STORED)
