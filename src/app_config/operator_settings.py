"""Global settings an admin edits, read from outside the app-config router.

The settings on ``AppConfig`` are mostly read by whoever renders them, straight
off the row. These are different: they are consulted deep inside a background
job, on a code path that must not fail because the database is briefly
unreachable and must not open a session per call. Same shape as
``app_default_timezone`` in ``src/schools/display_timezone.py`` — a nullable
column meaning "fall back to the env seed", behind a short in-process cache that
never raises, cleared by the router when the value is edited.

Living here rather than beside each consumer keeps the Vimeo client out of the
ORM: ``src/integrations`` talks to Vimeo, not to Postgres.
"""

from __future__ import annotations

import time

# Long enough that a sweep does one query, short enough that an edit lands
# everywhere within minutes. The process that made the edit clears it at once.
_TTL_SECONDS = 300

_audit_folder_cache: tuple[float, str | None] | None = None


def reset_vimeo_audit_folder_cache() -> None:
    """Drop the cached folder — called when an admin changes it."""
    global _audit_folder_cache
    _audit_folder_cache = None


def vimeo_audit_folder_uri() -> str | None:
    """The admin-set Vimeo audit folder, or None if unset or unreadable.

    Never raises. An unreadable config row means the caller falls through to the
    env seed, which is what every audit run used before this was editable.
    """
    global _audit_folder_cache
    now = time.monotonic()
    if _audit_folder_cache and now - _audit_folder_cache[0] < _TTL_SECONDS:
        return _audit_folder_cache[1]

    value: str | None = None
    try:
        # Imported here so importing this module costs nothing and cannot
        # participate in an import cycle through the ORM base.
        from sqlalchemy import select

        from src.app_config.models import AppConfig
        from src.db.base import get_session_factory

        with get_session_factory()() as db:
            value = db.scalar(select(AppConfig.vimeo_audit_folder_uri))
    except Exception:  # noqa: BLE001 - see docstring
        value = None

    _audit_folder_cache = (now, value)
    return value


__all__ = ["reset_vimeo_audit_folder_cache", "vimeo_audit_folder_uri"]
