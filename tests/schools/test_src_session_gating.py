"""Resource-center session tokens gate the public school endpoints."""

from datetime import datetime, timedelta, timezone

import pytest

from src.auth.deps import CurrentUser
from src.auth.rate_limit import _hits
from src.config import settings
from src.content.models import (
    ContentAsset, ContentAssetState, GradeConfig, GradeConfigGoal, GradeSet, Goal, Topic,
)
from src.main import app
from src.schools.models import School
from src.schools.src_session import optional_current_user
from src.workshops.models import Workshop
from tests.schools.src_test_support import install, make_session_factory, uid

FULL, OTHER, PREVIEW = uid(1), uid(2), uid(3)
T_ALLOWED, T_LOCKED = uid(10), uid(11)
A_PUBLIC, A_PRIVATE, A_RESTRICTED = uid(20), uid(21), uid(22)


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "src_session_secret", "unit-test-secret-not-real")
    monkeypatch.setattr(settings, "src_session_enforced", False)
    _hits.clear()


@pytest.fixture
def env():
    factory = make_session_factory()
    db = factory()
    future = datetime.now(timezone.utc) + timedelta(days=10)
    db.add_all([
        School(id=FULL, name="Full High", slug="full-high", is_current_customer=True, cmm_website_password="pw1"),
        School(id=OTHER, name="Other High", slug="other-high", is_current_customer=True),
        School(id=PREVIEW, name="Preview High", slug="preview-high", is_cmm_website_activated=True,
               is_src_preview=True, src_preview_expires_at=future, cmm_website_password="phcmm"),
    ])
    gs = GradeSet(id=uid(30), name="Default", is_default=True)
    goal = Goal(id=uid(31), name="Goal", slug="goal")
    cfg = GradeConfig(id=uid(32), grade_set_id=gs.id, grade=9, label="9th")
    db.add_all([gs, goal, cfg])
    db.flush()
    db.add(GradeConfigGoal(grade_config_id=cfg.id, goal_id=goal.id, sort_order=0))
    db.add_all([
        Topic(id=T_ALLOWED, title="A first", slug="a-first", status="published", goal_id=goal.id, sort_order=0,
              description="open"),
        Topic(id=T_LOCKED, title="B second", slug="b-second", status="published", goal_id=goal.id, sort_order=1,
              description="secret"),
        ContentAsset(id=A_PUBLIC, name="pub", status="published", is_public=True),
        ContentAsset(id=A_PRIVATE, name="priv", status="published", is_public=False),
        ContentAsset(id=A_RESTRICTED, name="restricted", status="published", is_public=True),
    ])
    db.flush()
    db.add(ContentAssetState(content_asset_id=A_RESTRICTED, state="TX"))
    for n in range(1, 8):
        db.add(Workshop(name=f"Workshop {n}", sequence_number=n))
    db.commit()
    db.close()
    client = install(factory)
    yield client
    app.dependency_overrides.clear()


def _token(client, slug, password="pw1"):
    resp = client.post(f"/api/v1/schools/slug/{slug}/verify-password", json={"password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_token"], resp.json()["mode"]


def _hdr(token):
    return {"X-SRC-Session": token}


def _code(resp):
    return resp.json()["detail"]["code"]


def test_verify_password_returns_mode(env):
    assert _token(env, "full-high")[1] == "full"
    assert _token(env, "preview-high", "phcmm")[1] == "preview"


def test_wrong_password_401(env):
    resp = env.post("/api/v1/schools/slug/full-high/verify-password", json={"password": "nope"})
    assert resp.status_code == 401


def test_no_password_school_gets_session_but_password_school_does_not(env):
    assert env.post("/api/v1/schools/slug/other-high/session").status_code == 200
    resp = env.post("/api/v1/schools/slug/full-high/session")
    assert resp.status_code == 401 and _code(resp) == "password_required"


def test_wrong_school_403(env):
    token, _ = _token(env, "full-high")
    resp = env.get("/api/v1/workshops/public/school/" + str(OTHER), headers=_hdr(token))
    assert resp.status_code == 403 and _code(resp) == "src_session_wrong_school"


def test_invalid_token_401_even_when_not_enforced(env):
    resp = env.get(f"/api/v1/content/assets/public?school_id={FULL}", headers=_hdr("garbage"))
    assert resp.status_code == 401 and _code(resp) == "src_session_invalid"


def test_full_school_without_token_allowed_unless_enforced(env, monkeypatch):
    assert env.get(f"/api/v1/content/assets/public?school_id={FULL}").status_code == 200
    monkeypatch.setattr(settings, "src_session_enforced", True)
    resp = env.get(f"/api/v1/content/assets/public?school_id={FULL}")
    assert resp.status_code == 401 and _code(resp) == "src_session_required"


def test_preview_school_always_needs_token(env):
    resp = env.get(f"/api/v1/content/assets/public?school_id={PREVIEW}")
    assert resp.status_code == 401 and _code(resp) == "src_session_required"


def test_password_change_voids_token(env):
    token, _ = _token(env, "full-high")
    assert env.get(f"/api/v1/content/assets/public?school_id={FULL}", headers=_hdr(token)).status_code == 200
    db = next(app.dependency_overrides[__import__("src.db.deps", fromlist=["get_db"]).get_db]())
    db.get(School, FULL).cmm_website_password = "changed"
    db.commit()
    resp = env.get(f"/api/v1/content/assets/public?school_id={FULL}", headers=_hdr(token))
    assert resp.status_code == 401 and _code(resp) == "src_session_invalid"


def test_preview_topic_locking(env):
    token, _ = _token(env, "preview-high", "phcmm")
    q = f"?school_id={PREVIEW}"
    assert env.get(f"/api/v1/content/topics/public/slug/a-first{q}", headers=_hdr(token)).status_code == 200
    resp = env.get(f"/api/v1/content/topics/public/slug/b-second{q}", headers=_hdr(token))
    assert resp.status_code == 403 and _code(resp) == "preview_locked"


def test_full_school_opens_every_topic(env):
    token, _ = _token(env, "full-high")
    resp = env.get(f"/api/v1/content/topics/public/slug/b-second?school_id={FULL}", headers=_hdr(token))
    assert resp.status_code == 200


def test_topic_without_school_id_requires_admin(env, monkeypatch):
    resp = env.get("/api/v1/content/topics/public/slug/a-first")
    assert resp.status_code == 401 and _code(resp) == "src_session_required"
    admin = CurrentUser(user_id=uid(500), email="a@x.com", role="super_admin", school_id=None)
    app.dependency_overrides[optional_current_user] = lambda: admin
    assert env.get("/api/v1/content/topics/public/slug/a-first").status_code == 200


def test_hub_user_of_same_school_acts_as_full(env):
    hub = CurrentUser(user_id=uid(501), email="h@x.com", role="hub_user", school_id=PREVIEW)
    app.dependency_overrides[optional_current_user] = lambda: hub
    resp = env.get(f"/api/v1/content/topics/public/slug/b-second?school_id={PREVIEW}")
    assert resp.status_code == 200


def test_hub_user_of_other_school_is_not_staff(env):
    hub = CurrentUser(user_id=uid(502), email="h@x.com", role="hub_user", school_id=OTHER)
    app.dependency_overrides[optional_current_user] = lambda: hub
    assert env.get(f"/api/v1/content/assets/public?school_id={PREVIEW}").status_code == 401


def test_preview_asset_tier(env):
    token, _ = _token(env, "preview-high", "phcmm")
    ids = {a["id"] for a in env.get(f"/api/v1/content/assets/public?school_id={PREVIEW}", headers=_hdr(token)).json()["items"]}
    assert ids == {str(A_PUBLIC)}
    for asset in (A_PRIVATE, A_RESTRICTED):
        resp = env.get(f"/api/v1/content/assets/{asset}/public?school_id={PREVIEW}", headers=_hdr(token))
        assert resp.status_code == 404
    assert env.get(f"/api/v1/content/assets/{A_PUBLIC}/public?school_id={PREVIEW}", headers=_hdr(token)).status_code == 200


def test_asset_without_school_id_limited_to_public_tier(env):
    assert env.get(f"/api/v1/content/assets/{A_PUBLIC}/public").status_code == 200
    assert env.get(f"/api/v1/content/assets/{A_PRIVATE}/public").status_code == 404
    assert env.get(f"/api/v1/content/assets/{A_RESTRICTED}/public").status_code == 404


def test_preview_workshops_are_placeholders(env):
    token, _ = _token(env, "preview-high", "phcmm")
    body = env.get(f"/api/v1/workshops/public/school/{PREVIEW}", headers=_hdr(token)).json()
    assert [w["webinar_id"] for w in body["upcoming"]] == [f"preview-{n}" for n in range(1, 7)]
    assert body["past"] == []
    assert all(w["is_preview"] and w["zoom_link"] is None and w["start_datetime"] is None for w in body["upcoming"])
    one = env.get(f"/api/v1/workshops/public/school/{PREVIEW}/webinar/preview-2", headers=_hdr(token))
    assert one.status_code == 200
    real = env.get(f"/api/v1/workshops/public/school/{PREVIEW}/webinar/{uid(77)}", headers=_hdr(token))
    assert real.status_code == 404


def test_expired_preview_403(env):
    token, _ = _token(env, "preview-high", "phcmm")
    db = next(app.dependency_overrides[__import__("src.db.deps", fromlist=["get_db"]).get_db]())
    db.get(School, PREVIEW).src_preview_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    resp = env.get(f"/api/v1/content/assets/public?school_id={PREVIEW}", headers=_hdr(token))
    assert resp.status_code == 403 and _code(resp) == "preview_expired"
    assert env.post("/api/v1/schools/slug/preview-high/verify-password", json={"password": "phcmm"}).status_code == 403


def test_conversion_to_customer_unlocks_existing_token(env):
    token, mode = _token(env, "preview-high", "phcmm")
    assert mode == "preview"
    db = next(app.dependency_overrides[__import__("src.db.deps", fromlist=["get_db"]).get_db]())
    db.get(School, PREVIEW).is_current_customer = True
    db.commit()
    resp = env.get(f"/api/v1/content/topics/public/slug/b-second?school_id={PREVIEW}", headers=_hdr(token))
    assert resp.status_code == 200


@pytest.mark.parametrize("environment", ["dev", "prod", "production"])
def test_missing_secret_fails_closed_when_deployed(monkeypatch, environment):
    from src.schools.src_session import _secret

    monkeypatch.setattr(settings, "src_session_secret", "")
    monkeypatch.setattr(settings, "environment", environment)
    with pytest.raises(RuntimeError):
        _secret()


def test_missing_secret_uses_dev_fallback_locally(monkeypatch):
    from src.schools.src_session import _secret

    monkeypatch.setattr(settings, "src_session_secret", "")
    monkeypatch.setattr(settings, "environment", "development")
    assert _secret()
