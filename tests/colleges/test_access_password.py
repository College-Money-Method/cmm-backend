"""scrypt hashing of the shared College Aid password."""

from __future__ import annotations

from src.colleges.access_password import hash_password, verify_password

FAKE = "fake-pass-1234"


def test_round_trip():
    assert verify_password(FAKE, hash_password(FAKE))


def test_wrong_password_rejected():
    assert not verify_password("other-pass-1234", hash_password(FAKE))


def test_salted_and_not_plaintext():
    a, b = hash_password(FAKE), hash_password(FAKE)
    assert a != b
    assert FAKE not in a
    assert a.count("$") == 1


def test_malformed_or_missing_stored_value_is_false():
    for stored in (None, "", "nodollar", "zz$zz", "$"):
        assert not verify_password(FAKE, stored)
