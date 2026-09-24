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

# Every folder the pipeline uploads into, read in one query and cached together.
_FOLDER_COLUMNS = ("vimeo_audit_folder_uri", "vimeo_replay_folder_uri", "vimeo_reel_folder_uri")

_folder_cache: tuple[float, dict[str, str | None]] | None = None


def reset_vimeo_folder_cache() -> None:
    """Drop the cached folders — called when an admin changes one."""
    global _folder_cache
    _folder_cache = None


def _folders() -> dict[str, str | None]:
    """The admin-set Vimeo folders, each None if unset; all None if unreadable.

    Never raises. An unreadable config row means the caller falls through to the
    env seed, which is what every upload used before these were editable.
    """
    global _folder_cache
    now = time.monotonic()
    if _folder_cache and now - _folder_cache[0] < _TTL_SECONDS:
        return _folder_cache[1]

    values: dict[str, str | None] = dict.fromkeys(_FOLDER_COLUMNS)
    try:
        # Imported here so importing this module costs nothing and cannot
        # participate in an import cycle through the ORM base.
        from sqlalchemy import select

        from src.app_config.models import AppConfig
        from src.db.base import get_session_factory

        columns = [getattr(AppConfig, name) for name in _FOLDER_COLUMNS]
        with get_session_factory()() as db:
            row = db.execute(select(*columns)).first()
        if row is not None:
            values = dict(zip(_FOLDER_COLUMNS, row))
    except Exception:  # noqa: BLE001 - see docstring
        pass

    _folder_cache = (now, values)
    return values


def vimeo_audit_folder_uri() -> str | None:
    """The admin-set folder audit runs upload into."""
    return _folders()["vimeo_audit_folder_uri"]


def vimeo_replay_folder_uri() -> str | None:
    """The admin-set folder production replays upload into."""
    return _folders()["vimeo_replay_folder_uri"]


def vimeo_reel_folder_uri() -> str | None:
    """The admin-set folder trailer reels upload into."""
    return _folders()["vimeo_reel_folder_uri"]


__all__ = [
    "reset_vimeo_folder_cache",
    "vimeo_audit_folder_uri",
    "vimeo_reel_folder_uri",
    "vimeo_replay_folder_uri",
]
