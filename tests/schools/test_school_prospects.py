"""Public fit-check intake: duplicate check, preview creation, admin list."""

import io
import json
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from src.auth.deps import require_admin
from src.auth.rate_limit import _hits
from src.config import settings
from src.db.client import get_supabase  # noqa: F401
from src.guest_contacts.models import GuestContact
from src.main import app
from src.schools.models import School
from src.schools.prospect_matching import initials_password, normalize_school_name
from src.schools.src_session import _decode
from src.storage.s3_client import get_s3_client
from tests.schools.src_test_support import install, make_session_factory, uid



def _png(width=4, height=4) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


PNG = _png()
FORM = {
    "school_name": "Sunrise Valley Academy", "city": "Austin", "state": "tx",
    "first_name": "Maria", "last_name": "Lopez", "email": "maria.lopez@sunrise.org",
    "role": "Counselor",
    "quiz_answers": json.dumps({"grade": ["9", "10"], "goal": "scholarships"}),
}


class FakeS3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(settings, "src_session_secret", "unit-test-secret-not-real")
    _hits.clear()
    factory = make_session_factory()
    s3 = FakeS3()
    client = install(factory)
    app.dependency_overrides[get_s3_client] = lambda: s3
    client.factory, client.s3 = factory, s3
    yield client
    app.dependency_overrides.clear()
    _hits.clear()


def _post(client, logo=None, **over):
    files = {"logo": ("logo.png", io.BytesIO(logo[0]), logo[1])} if logo else None
    return client.post("/api/v1/schools/prospects", data={**FORM, **over}, files=files)


@pytest.mark.parametrize("name,expected", [
    ("Baylor School", "bscmm"),
    ("Abraham Lincoln High School", "alhscmm"),
    ("The School of the Arts", "sacmm"),
    ("St. Mary's & Sons Academy", "smssacmm"),
])
def test_initials_password(name, expected):
    assert initials_password(name) == expected


def test_normalize_school_name():
    assert normalize_school_name("  St. Mary's   HIGH-School ") == "st mary s high school"


def test_creates_preview_school(env):
    resp = _post(env)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "preview_ready"
    assert body["password"] == "svacmm"
    assert body["slug"] == "sunrise-valley-academy"
    claims = _decode(body["session_token"])
    assert claims["mode"] == "preview" and claims["sub"] == body["school_id"]
    db = env.factory()
    school = db.query(School).one()
    assert school.state == "TX" and school.is_src_preview and school.is_cmm_website_activated
    assert not school.is_current_customer and school.prospect_source == "landing_fit_check"
    expires = school.src_preview_expires_at.replace(tzinfo=timezone.utc)
    assert timedelta(days=29) < expires - datetime.now(timezone.utc) < timedelta(days=31)
    contact = db.query(GuestContact).one()
    assert contact.school_id == school.id and contact.source == "school_fit_check"
    assert contact.quiz_answers["goal"] == "scholarships" and "scholarships" in contact.message
    assert not contact.is_spam


def test_check_reflects_state(env):
    q = "/api/v1/schools/prospects/check?name=Sunrise%20Valley%20Academy&state=TX"
    assert env.get(q).json()["status"] == "available"
    _post(env)
    body = env.get(q).json()
    assert body["status"] == "preview_exists" and body["slug"] == "sunrise-valley-academy"
    assert env.get(q.replace("TX", "CA")).json()["status"] == "available"


def test_existing_customer_untouched(env):
    db = env.factory()
    db.add(School(id=uid(1), name="Sunrise Valley Academy", slug="sunrise", state="TX",
                  is_current_customer=True, cmm_website_password="orig"))
    db.commit()
    body = _post(env).json()
    assert body["status"] == "existing_partner" and body["password"] is None and body["session_token"] is None
    db = env.factory()
    school = db.query(School).one()
    assert school.cmm_website_password == "orig" and not school.is_src_preview
    assert db.query(GuestContact).one().school_id == school.id


def test_admin_activated_prospect_is_not_modified(env):
    db = env.factory()
    db.add(School(id=uid(1), name="Sunrise Valley Academy", slug="sunrise", state="TX",
                  is_cmm_website_activated=True, cmm_website_password="orig"))
    db.commit()
    assert _post(env).json()["status"] == "existing_partner"
    assert not env.factory().query(School).one().is_src_preview


def test_existing_preview_returns_slug_without_password(env):
    _post(env)
    body = _post(env, email="other@sunrise.org").json()
    assert body["status"] == "preview_exists" and body["slug"] == "sunrise-valley-academy"
    assert body["password"] is None and body["session_token"] is None


def test_expired_preview_reported(env):
    _post(env)
    db = env.factory()
    db.query(School).one().src_preview_expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    db.commit()
    assert _post(env, email="o@sunrise.org").json()["status"] == "preview_expired"


def test_unactivated_pipeline_row_gets_preview(env):
    db = env.factory()
    db.add(School(id=uid(1), name="Sunrise Valley Academy", slug="sunrise", state="TX"))
    db.commit()
    assert env.get("/api/v1/schools/prospects/check?name=Sunrise%20Valley%20Academy&state=TX").json()["status"] == "available"
    body = _post(env).json()
    assert body["status"] == "preview_ready" and body["slug"] == "sunrise" and body["password"] == "svacmm"
    db = env.factory()
    assert db.query(School).count() == 1 and db.query(School).one().is_src_preview


def test_honeypot_saved_as_spam_without_school(env):
    body = _post(env, website="http://spam.example").json()
    assert body["status"] == "existing_partner" and body["school_id"] is None
    db = env.factory()
    assert db.query(School).count() == 0
    contact = db.query(GuestContact).one()
    assert contact.is_spam and contact.school_id is None


def test_logo_uploaded_and_stored(env):
    resp = _post(env, logo=(PNG, "image/png"))
    assert resp.status_code == 201
    assert env.s3.puts and env.s3.puts[0]["ContentType"] == "image/png"
    assert env.factory().query(School).one().logo_url


def test_svg_logo_rejected_and_nothing_created(env):
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>"
    resp = _post(env, logo=(svg, "image/svg+xml"))
    assert resp.status_code == 422
    assert env.factory().query(School).count() == 0


def test_spoofed_content_type_rejected(env):
    assert _post(env, logo=(b"not an image at all", "image/png")).status_code == 422


def test_logo_with_bad_body_rejected(env):
    assert _post(env, logo=(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, "image/png")).status_code == 422


def test_logo_with_huge_dimensions_rejected(env):
    resp = _post(env, logo=(_png(5000, 5000), "image/png"))
    assert resp.status_code == 422 and resp.json()["detail"]["code"] == "invalid_logo"
    assert "pixels" in resp.json()["detail"]["message"]
    assert env.factory().query(School).count() == 0


def test_oversize_logo_rejected(env):
    big = PNG + b"\x00" * (2 * 1024 * 1024)
    assert _post(env, logo=(big, "image/png")).status_code == 422


@pytest.mark.parametrize("over", [
    {"state": "Texas"}, {"email": "not-an-email"}, {"quiz_answers": "[1,2]"}, {"quiz_answers": "{bad"},
])
def test_validation_errors(env, over):
    assert _post(env, **over).status_code == 422


def test_submit_rate_limited(env):
    codes = [_post(env, email=f"u{i}@sunrise.org").status_code for i in range(4)]
    assert codes[:3] == [201, 201, 201] and codes[3] == 429


def test_check_rate_limited(env):
    codes = [env.get("/api/v1/schools/prospects/check?name=x&state=TX").status_code for i in range(21)]
    assert codes[-1] == 429 and set(codes[:-1]) == {200}


def test_recent_requires_admin(env):
    assert env.get("/api/v1/schools/prospects/recent").status_code in (401, 403)


def test_recent_lists_newest_first_with_contact(env):
    _post(env)
    _post(env, school_name="Zephyr Prep", first_name="Sam", last_name="Lee", email="sam@zephyr.org")
    app.dependency_overrides[require_admin] = lambda: object()
    rows = env.get("/api/v1/schools/prospects/recent?limit=5").json()
    assert {r["school_name"] for r in rows} == {"Sunrise Valley Academy", "Zephyr Prep"}
    zephyr = next(r for r in rows if r["school_name"] == "Zephyr Prep")
    assert zephyr["contact_name"] == "Sam Lee" and zephyr["email"] == "sam@zephyr.org" and zephyr["is_src_preview"]


def test_admin_filters_for_fit_check_prospects(env):
    from src.auth.deps import get_current_user
    from src.auth.schemas import CurrentUser

    school_id = _post(env).json()["school_id"]
    _post(env, school_name="Zephyr Prep", first_name="Sam", last_name="Lee", email="sam@zephyr.org")
    app.dependency_overrides[require_admin] = lambda: object()
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id=uid(999), email="admin@example.org", role="super_admin"
    )

    schools = env.get("/api/v1/schools?prospect_source=landing_fit_check").json()["items"]
    assert {s["name"] for s in schools} == {"Sunrise Valley Academy", "Zephyr Prep"}
    assert all(s["is_src_preview"] for s in schools)
    assert env.get("/api/v1/schools?prospect_source=airtable").json()["items"] == []

    contacts = env.get(f"/api/v1/guest-contacts?school_id={school_id}").json()
    assert [c["email"] for c in contacts] == ["maria.lopez@sunrise.org"]
    assert contacts[0]["source"] == "school_fit_check"
    assert contacts[0]["quiz_answers"] == json.loads(FORM["quiz_answers"])
