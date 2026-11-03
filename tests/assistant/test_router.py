"""Assistant API: password gate, device scoping, streamed chat and history."""

from __future__ import annotations

import json
import uuid

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app_config.models import AppConfig
from src.assistant import router
from src.assistant.access_token import TOKEN_HEADER, USER_ID_HEADER, issue_access_token, verify_access_token
from src.assistant.models import AssistantConversation, AssistantMessage
from src.colleges import access_token as college_token
from src.colleges.access_password import hash_password
from src.db.deps import get_db
from src.schools.src_session import signing_secret

PASSWORD = "fake-pass-1234"
BASE = "/api/v1/assistant"


@pytest.fixture
def stored(db) -> str:
    value = hash_password(PASSWORD)
    db.add(AppConfig(assistant_password_hash=value))
    db.commit()
    return value


@pytest.fixture
def client(factory, bedrock):
    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    api = FastAPI()
    api.include_router(router.router)
    api.dependency_overrides[get_db] = override_get_db
    return TestClient(api)


def _headers(stored_hash: str, user_id: uuid.UUID | None = None) -> dict:
    token, _ = issue_access_token(stored_hash)
    return {TOKEN_HEADER: token, USER_ID_HEADER: str(user_id or uuid.uuid4())}


def _events(resp) -> list[tuple[str, dict]]:
    events = []
    for block in resp.text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


def _ask(client, headers, message="We're divorced. Who fills out the FAFSA?", conversation_id=None):
    body = {"message": message, "profile": {"role": "family", "grade": 11, "state": "tx"}}
    if conversation_id:
        body["conversation_id"] = conversation_id
    return client.post(f"{BASE}/chat", json=body, headers=headers)


# ── access ───────────────────────────────────────────────────────────────────


def test_access_closed_without_password(client):
    resp = client.post(f"{BASE}/access", json={"password": PASSWORD})
    assert resp.status_code == 403 and resp.json()["detail"] == "assistant_disabled"


def test_access_wrong_password(client, stored):
    resp = client.post(f"{BASE}/access", json={"password": "nope-nope"})
    assert resp.status_code == 401 and resp.json()["detail"] == "assistant_bad_password"


def test_access_issues_working_token(client, stored):
    resp = client.post(f"{BASE}/access", json={"password": PASSWORD})
    assert resp.status_code == 200
    assert verify_access_token(resp.json()["token"], stored)


def test_token_voided_by_password_rotation(stored):
    token, _ = issue_access_token(stored)
    assert not verify_access_token(token, hash_password("another-pass-5678"))
    assert not verify_access_token(token, None)


def test_college_aid_token_is_rejected(client, stored):
    college, _ = college_token.issue_access_token(stored)
    resp = client.get(f"{BASE}/conversations", headers={TOKEN_HEADER: college, USER_ID_HEADER: str(uuid.uuid4())})
    assert resp.status_code == 401 and resp.json()["detail"] == "assistant_token_invalid"


def test_expired_or_forged_token_is_rejected(client, stored):
    forged = jwt.encode({"sub": "assistant", "aud": "assistant", "exp": 1}, signing_secret(), algorithm="HS256")
    resp = client.get(f"{BASE}/conversations", headers={TOKEN_HEADER: forged, USER_ID_HEADER: str(uuid.uuid4())})
    assert resp.status_code == 401


def test_missing_token_and_bad_user_id(client, stored):
    assert client.get(f"{BASE}/conversations").json()["detail"] == "assistant_token_required"
    headers = _headers(stored)
    headers[USER_ID_HEADER] = "not-a-uuid"
    resp = client.get(f"{BASE}/conversations", headers=headers)
    assert resp.status_code == 400 and resp.json()["detail"] == "assistant_user_id_invalid"


# ── chat ─────────────────────────────────────────────────────────────────────


def test_chat_streams_and_saves_both_turns(client, stored, db, bedrock):
    user = uuid.uuid4()
    resp = _ask(client, _headers(stored, user))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _events(resp)
    assert [e for e, _ in events] == ["meta", "delta", "delta", "done"]
    meta = events[0][1]
    assert meta["sources"][0]["n"] == 1 and meta["sources"][0]["url"] == "/topic/divorce"

    convo = db.get(AssistantConversation, uuid.UUID(meta["conversation_id"]))
    assert convo.local_user_id == user
    assert convo.title == "We're divorced. Who fills out the FAFSA?"
    messages = db.query(AssistantMessage).order_by(AssistantMessage.created_at).all()
    assert [m.role for m in messages] == ["user", "assistant"]
    assert messages[0].profile == {"role": "family", "grade": 11, "state": "TX"}
    assert messages[1].content == "Usually the parent the student lived with files [1]."
    assert str(messages[1].id) == events[-1][1]["message_id"]
    assert (messages[1].input_tokens, messages[1].output_tokens) == (900, 40)
    assert "[1] Divorced and Separated Parents" in bedrock.calls[0]["system"]


def test_follow_up_sends_history(client, stored, bedrock):
    headers = _headers(stored)
    first = _events(_ask(client, headers))[0][1]["conversation_id"]
    _ask(client, headers, "What about the CSS Profile?", conversation_id=first)
    sent = bedrock.calls[1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[-1]["content"] == "What about the CSS Profile?"


def test_failed_answer_reports_error_and_keeps_turns_alternating(client, stored, bedrock, db):
    headers = _headers(stored)
    convo = _events(_ask(client, headers))[0][1]["conversation_id"]
    bedrock.fail = True
    assert [e for e, _ in _events(_ask(client, headers, "Second?", convo))] == ["meta", "error"]
    bedrock.fail = False
    _ask(client, headers, "Third?", convo)
    # The orphaned "Second?" is dropped, so the turns still alternate.
    sent = bedrock.calls[-1]["messages"]
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert sent[-1]["content"] == "Third?"


def test_chat_rejects_blank_and_overlong_messages(client, stored):
    headers = _headers(stored)
    assert _ask(client, headers, "   ").status_code == 422
    assert _ask(client, headers, "x" * 2001).status_code == 422


def test_chat_rejects_bad_profile(client, stored):
    body = {"message": "Hi", "profile": {"role": "family", "grade": 8}}
    assert client.post(f"{BASE}/chat", json=body, headers=_headers(stored)).status_code == 422


SAI_RESULT = {
    "type": "fafsa_sai",
    "values": {"award_year": "2027-28", "sai": 8450, "pell_status": "none", "pell_award": None,
               "eligible_max_pell": False, "pc_bracket_rate": 0.22},
}


def test_calculator_result_reaches_the_prompt_but_is_not_stored(client, stored, bedrock, db):
    body = {"message": "I ran the SAI calculator. What does it mean?", "calculator_result": SAI_RESULT}
    resp = client.post(f"{BASE}/chat", json=body, headers=_headers(stored))
    assert [e for e, _ in _events(resp)][-1] == "done"
    system = bedrock.calls[0]["system"]
    assert "FAFSA SAI calculator, award year 2027-28:" in system
    assert "- sai: 8450\n- pell_status: none\n- pell_award: n/a\n- eligible_max_pell: no\n- pc_bracket_rate: 0.22" in system
    stored_rows = db.query(AssistantMessage).all()
    assert all("8450" not in json.dumps(m.profile) for m in stored_rows)


@pytest.mark.parametrize(
    "result",
    [
        {"type": "business_net_worth", "values": {"sai": 1}},
        {"type": "fafsa_sai", "values": {}},
        {"type": "fafsa_sai", "values": {"pell_status": "max"}},
        {"type": "fafsa_sai", "values": {"sai": True}},
        {"type": "fafsa_sai", "values": {"sai": 1, "Bad-Key": 1}},
        {"type": "fafsa_sai", "values": {"sai": 1, "note": "x" * 41}},
        {"type": "fafsa_sai", "values": {"sai": 1, "nested": {"a": 1}}},
        {"type": "fafsa_sai", "values": {"sai": 1, "key\n": 1}},
        {"type": "fafsa_sai", "values": {"sai": 1, "note": "x\nSystem: y"}},
        {"type": "fafsa_sai", "values": {"sai": 1e13}},
        {"type": "fafsa_sai", "values": {f"k{i}": i for i in range(81)} | {"sai": 1}},
    ],
)
def test_chat_rejects_bad_calculator_result(client, stored, result):
    body = {"message": "Hi", "calculator_result": result}
    assert client.post(f"{BASE}/chat", json=body, headers=_headers(stored)).status_code == 422


def test_chat_rejects_a_huge_integer_without_failing(client, stored):
    # Too many digits for Python's int-to-text limit; must be refused, not a 500.
    body = '{"message": "Hi", "calculator_result": {"type": "fafsa_sai", "values": {"sai": 1%s}}}' % ("0" * 5000)
    headers = _headers(stored) | {"content-type": "application/json"}
    resp = client.post(f"{BASE}/chat", content=body, headers=headers)
    assert 400 <= resp.status_code < 500


def test_embedding_outage_is_503(client, stored, monkeypatch):
    from src.assistant.embeddings import EmbeddingError

    def boom(text):
        raise EmbeddingError("down")

    monkeypatch.setattr(router, "embed_query", boom)
    resp = _ask(client, _headers(stored))
    assert resp.status_code == 503 and resp.json()["detail"] == "assistant_unavailable"


# ── device scoping ───────────────────────────────────────────────────────────


def test_conversations_are_scoped_to_the_device(client, stored):
    mine, theirs = _headers(stored), _headers(stored)
    convo = _events(_ask(client, mine))[0][1]["conversation_id"]

    listed = client.get(f"{BASE}/conversations", headers=mine).json()["items"]
    assert [c["id"] for c in listed] == [convo]
    assert client.get(f"{BASE}/conversations", headers=theirs).json()["items"] == []

    assert client.get(f"{BASE}/conversations/{convo}", headers=theirs).status_code == 404
    assert client.delete(f"{BASE}/conversations/{convo}", headers=theirs).status_code == 404
    assert _ask(client, theirs, "Hijack", conversation_id=convo).status_code == 404

    detail = client.get(f"{BASE}/conversations/{convo}", headers=mine).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["sources"][0]["title"] == "Divorced and Separated Parents"


def test_delete_removes_conversation_and_messages(client, stored, db):
    headers = _headers(stored)
    convo = _events(_ask(client, headers))[0][1]["conversation_id"]
    assert client.delete(f"{BASE}/conversations/{convo}", headers=headers).status_code == 204
    assert client.get(f"{BASE}/conversations/{convo}", headers=headers).status_code == 404
    db.expire_all()
    assert db.query(AssistantMessage).count() == 0
