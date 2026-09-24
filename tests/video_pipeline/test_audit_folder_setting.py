"""Where an audit run uploads, once an admin can change it.

The folder used to come only from the environment, so moving this month's audits
somewhere else meant a deploy. It is a Global Setting now, and the risk that
comes with that is a cleared field reading as "upload into an empty folder URI"
rather than "use the seed" — an audit that lands in the main library beside the
production replays is exactly what the audit folder exists to prevent. So the
blank cases are tested as carefully as the configured one.
"""

from __future__ import annotations

import pytest

from pydantic import ValidationError

from src.app_config import operator_settings
from src.app_config.models import AppConfig
from src.app_config.schemas import AppConfigUpdate
from src.config import settings
from src.integrations.vimeo_upload import audit_folder_uri, reel_folder_uri, replay_folder_uri

# Captured at import, before the suite-wide fixture in ``tests/conftest.py``
# stubs the name out: the database tests below need the real reader back.
_real_lookup = operator_settings.vimeo_audit_folder_uri
_real_replay_lookup = operator_settings.vimeo_replay_folder_uri
_real_reel_lookup = operator_settings.vimeo_reel_folder_uri

SEED = "/users/151255816/projects/11111111"
CHOSEN = "/users/151255816/projects/99999999"


@pytest.fixture
def seeded(monkeypatch):
    monkeypatch.setattr(settings, "vimeo_audit_folder_uri", SEED)


@pytest.fixture
def override(monkeypatch):
    """Set the admin-editable value, bypassing the database."""

    def _set(value: str | None) -> None:
        monkeypatch.setattr(operator_settings, "vimeo_audit_folder_uri", lambda: value)

    return _set


# ── which value wins ─────────────────────────────────────────────────────────


def test_the_env_seed_applies_until_an_admin_sets_a_folder(seeded):
    assert audit_folder_uri() == SEED


def test_an_admin_set_folder_wins_over_the_env_seed(seeded, override):
    override(CHOSEN)

    assert audit_folder_uri() == CHOSEN


def test_a_cleared_field_falls_back_to_the_seed_rather_than_to_nothing(seeded, override):
    """The field has to be clearable, and clearing it must not disable the
    guard that keeps audits out of the main library."""
    override("")
    assert audit_folder_uri() == SEED

    override("   ")
    assert audit_folder_uri() == SEED


def test_neither_configured_means_no_folder(monkeypatch, override):
    """Which the callers read as "refuse to run" — asserted in their own tests."""
    monkeypatch.setattr(settings, "vimeo_audit_folder_uri", "")
    override(None)

    assert audit_folder_uri() == ""


def test_a_pasted_folder_uri_keeps_no_trailing_slash(override):
    """Vimeo builds ``{uri}/videos`` from this, so a stray slash is a 404."""
    override(f"{CHOSEN}/")

    assert audit_folder_uri() == CHOSEN


# ── what an admin is allowed to paste ────────────────────────────────────────


@pytest.mark.parametrize(
    "pasted",
    [
        "https://vimeo.com/user/151255816/folder/30467578",
        "https://vimeo.com/user/151255816/folder/30467578?isPrivate=false",
        "vimeo.com/user/151255816/folder/30467578",
        "/users/151255816/projects/30467578",
        "/users/151255816/projects/30467578/",
    ],
)
def test_a_folder_is_stored_as_an_api_uri_however_it_was_pasted(pasted):
    """An admin picking a folder is looking at its Vimeo page, so that address is
    what they will copy. Converting it beats rejecting it: the two forms share
    their ids, and the rejection would otherwise be discovered by an audit run
    that has already downloaded and trimmed the recording."""
    body = AppConfigUpdate(vimeo_audit_folder_uri=pasted)

    assert body.vimeo_audit_folder_uri == "/users/151255816/projects/30467578"


def test_a_cleared_field_is_stored_as_no_override():
    assert AppConfigUpdate(vimeo_audit_folder_uri="   ").vimeo_audit_folder_uri is None


def test_something_that_names_no_folder_is_refused_at_the_form():
    with pytest.raises(ValidationError):
        AppConfigUpdate(vimeo_audit_folder_uri="the audit folder")


# ── reading it out of the database ───────────────────────────────────────────


@pytest.fixture
def live_config(monkeypatch, sessionmaker_factory):
    """Point the real lookup at this test's database instead of the app's."""
    import src.db.base as db_base

    monkeypatch.setattr(operator_settings, "vimeo_audit_folder_uri", _real_lookup)
    monkeypatch.setattr(db_base, "get_session_factory", lambda url=None: sessionmaker_factory)
    operator_settings.reset_vimeo_folder_cache()
    yield sessionmaker_factory
    operator_settings.reset_vimeo_folder_cache()


def test_the_stored_folder_is_what_the_pipeline_reads(seeded, live_config):
    with live_config() as db:
        # The row itself is seeded by ``sessionmaker_factory``.
        db.query(AppConfig).update({"vimeo_audit_folder_uri": CHOSEN})
        db.commit()

    assert audit_folder_uri() == CHOSEN


def test_an_unreadable_config_row_reports_unset_instead_of_raising(monkeypatch):
    """A database that is briefly down must not fail an audit run at the paste;
    the caller falls through to the env seed it always had."""
    import src.db.base as db_base

    def _boom(url=None):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(operator_settings, "_folder_cache", None)
    monkeypatch.setattr(db_base, "get_session_factory", _boom)

    assert operator_settings.vimeo_audit_folder_uri() is None


def test_an_edit_is_visible_before_the_cache_would_have_expired(seeded, live_config):
    """Which is why the PATCH handler calls the reset — five minutes of an admin
    watching nothing happen reads as a broken setting."""
    assert audit_folder_uri() == SEED  # caches the empty override

    with live_config() as db:
        db.query(AppConfig).update({"vimeo_audit_folder_uri": CHOSEN})
        db.commit()

    assert audit_folder_uri() == SEED
    operator_settings.reset_vimeo_folder_cache()
    assert audit_folder_uri() == CHOSEN


def test_replays_and_reels_read_their_own_folders_off_the_same_row(monkeypatch, live_config):
    """Replays and reels follow the audit folder's rules: the admin's value wins,
    and a blank one falls back to the env seed."""
    monkeypatch.setattr(operator_settings, "vimeo_replay_folder_uri", _real_replay_lookup)
    monkeypatch.setattr(operator_settings, "vimeo_reel_folder_uri", _real_reel_lookup)
    monkeypatch.setattr(settings, "vimeo_replay_folder_uri", SEED)
    monkeypatch.setattr(settings, "vimeo_reel_folder_uri", "")
    with live_config() as db:
        db.query(AppConfig).update({"vimeo_replay_folder_uri": "  ",
                                    "vimeo_reel_folder_uri": CHOSEN})
        db.commit()

    assert replay_folder_uri() == SEED
    assert reel_folder_uri() == CHOSEN
