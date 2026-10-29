"""Hash and verify the College Aid Explorer shared password.

Stored on the app_config singleton as ``salt$hash`` (hex), scrypt via stdlib so
no new dependency. The plaintext is never stored, logged or returned.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# scrypt cost: ~16 MiB, tens of ms; fine for a rarely-hit gate.
_N, _R, _P = 2**14, 8, 1
_SALT_BYTES = 16
_KEY_BYTES = 32


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_KEY_BYTES)


def hash_password(password: str) -> str:
    """Return ``salt$hash`` (hex) with a fresh random salt."""
    salt = secrets.token_bytes(_SALT_BYTES)
    return f"{salt.hex()}${_derive(password, salt).hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check; False for a missing or malformed stored value."""
    if not stored or "$" not in stored:
        return False
    salt_hex, _, hash_hex = stored.partition("$")
    try:
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except ValueError:
        return False
    return hmac.compare_digest(_derive(password, salt), expected)
