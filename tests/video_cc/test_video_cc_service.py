"""Job-runner tests: event sequence, partial failure isolation, fatal aborts.

Bedrock and Vimeo are stubbed. The behaviour under test is orchestration — in
particular that one language failing never prevents the others from publishing,
and that a job can never raise out of `run_job` (it would kill the event loop
task with no error surfaced to the streaming client).
"""

import asyncio

import pytest

from src.content import video_cc_service as svc
from src.content.bedrock_translation import TranslationError, TranslationOutput
from src.content.video_cc_jobs import create_job
from src.integrations.vimeo import VimeoError

VTT = "WEBVTT\n\n" + "\n\n".join(
    f"{i}\n00:00:{i:02d}.000 --> 00:00:{i + 1:02d}.000\nCue number {i}."
    for i in range(1, 13)
)


class FakeSession:
    """Minimal Session stand-in — the ledger write itself is stubbed out."""

    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    def add(self, _obj):
        pass

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


@pytest.fixture
def stubs(monkeypatch):
    """Wire up fake Bedrock + Vimeo and record what reached each."""
    state = {"uploads": [], "deleted": [], "usage": [], "session": FakeSession()}

    monkeypatch.setattr(
        svc,
        "translate_fields",
        lambda fields, locale: TranslationOutput(
            fields={k: f"[{locale}] {v}" for k, v in fields.items()},
            input_tokens=100,
            output_tokens=200,
            model_id="stub-haiku",
        ),
    )
    monkeypatch.setattr(
        svc,
        "record_translation_usage",
        lambda db, context, locale, out, count: state["usage"].append((context, locale, count)),
    )
    monkeypatch.setattr(svc, "get_session_factory", lambda: (lambda: state["session"]))
    monkeypatch.setattr(svc.vimeo, "get_video_name", lambda ref: "FAFSA Walkthrough")
    monkeypatch.setattr(
        svc.vimeo,
        "resolve_language",
        lambda locale, name: {"es": ("es", "Spanish"), "zh": ("zh-Hans", "Chinese")}[locale],
    )
    monkeypatch.setattr(svc.vimeo, "list_text_tracks", lambda ref: [])
    monkeypatch.setattr(
        svc.vimeo, "delete_text_track", lambda uri: state["deleted"].append(uri)
    )
    monkeypatch.setattr(
        svc.vimeo,
        "upload_text_track",
        lambda ref, lang, name, content, active=True: (
            state["uploads"].append((lang, content)) or f"/videos/1/texttracks/{lang}"
        ),
    )
    return state


def types_of(job):
    return [e["type"] for e in job.events]


def test_happy_path_publishes_every_language(stubs):
    job = create_job("123:hash")
    asyncio.run(svc.run_job(job, VTT, ["es", "zh"]))

    assert job.status == "completed"
    assert types_of(job)[-1] == "done"
    assert job.events[-1]["succeeded"] == ["es", "zh"]
    assert [lang for lang, _ in stubs["uploads"]] == ["es", "zh-Hans"]


def test_timings_survive_into_the_uploaded_track(stubs):
    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    _, body = stubs["uploads"][0]
    assert body.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:02.000" in body
    assert "[es] Cue number 1." in body


def test_usage_is_recorded_under_the_video_cc_context(stubs):
    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert stubs["usage"], "every Bedrock call must hit the ledger"
    assert all(context == "video_cc" for context, _, _ in stubs["usage"])
    assert all(locale == "es" for _, locale, _ in stubs["usage"])
    # Cue counts across chunks must add up to the transcript length.
    assert sum(count for _, _, count in stubs["usage"]) == 12


def test_existing_track_in_same_language_is_replaced(stubs, monkeypatch):
    monkeypatch.setattr(
        svc.vimeo,
        "list_text_tracks",
        lambda ref: [
            {"uri": "/videos/1/texttracks/old-es", "language": "es"},
            {"uri": "/videos/1/texttracks/keep-fr", "language": "fr"},
        ],
    )
    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    # Only the matching language is removed; unrelated tracks are left alone.
    assert stubs["deleted"] == ["/videos/1/texttracks/old-es"]
    assert job.events[-2]["replaced"] is True


def test_one_language_failing_does_not_block_the_other(stubs, monkeypatch):
    def upload(ref, lang, name, content, active=True):
        if lang == "zh-Hans":
            raise VimeoError("Vimeo denied access (403).", status=403)
        stubs["uploads"].append((lang, content))
        return f"/videos/1/texttracks/{lang}"

    monkeypatch.setattr(svc.vimeo, "upload_text_track", upload)

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es", "zh"]))

    assert job.events[-1]["succeeded"] == ["es"]
    assert job.events[-1]["failed"][0]["locale"] == "zh"
    assert "403" in job.events[-1]["failed"][0]["error"]
    assert [lang for lang, _ in stubs["uploads"]] == ["es"]
    # A successful language still counts as a completed job.
    assert job.status == "completed"


def test_bedrock_failure_is_reported_and_rolled_back(stubs, monkeypatch):
    def boom(fields, locale):
        raise TranslationError("Bedrock throttled (429)")

    monkeypatch.setattr(svc, "translate_fields", boom)

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "failed"
    assert "language_error" in types_of(job)
    assert stubs["session"].rollbacks == 1
    assert not stubs["uploads"], "nothing may reach Vimeo when translation fails"


def test_missing_video_aborts_before_any_spend(stubs, monkeypatch):
    def missing(ref):
        raise VimeoError("Video not found on Vimeo.", status=404)

    monkeypatch.setattr(svc.vimeo, "get_video_name", missing)

    job = create_job("999")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "failed"
    assert job.events[-1]["type"] == "error"
    assert not stubs["usage"] and not stubs["uploads"]


def test_unparseable_transcript_aborts_before_any_spend(stubs):
    job = create_job("123")
    asyncio.run(svc.run_job(job, "this is not a caption file", ["es"]))

    assert job.status == "failed"
    assert job.events[-1]["type"] == "error"
    assert not stubs["usage"] and not stubs["uploads"]


def test_unexpected_error_never_escapes_the_runner(stubs, monkeypatch):
    """run_job is a detached task — an escaping exception would vanish silently."""

    def explode(ref):
        raise RuntimeError("something nobody predicted")

    monkeypatch.setattr(svc.vimeo, "get_video_name", explode)

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))  # must not raise

    assert job.status == "failed"
    assert job.events[-1]["type"] == "error"


def test_cues_missing_from_the_model_response_keep_english(stubs, monkeypatch):
    """Half the keys come back; the rest must fall back, not blank out."""
    monkeypatch.setattr(
        svc,
        "translate_fields",
        lambda fields, locale: TranslationOutput(
            fields={k: f"[{locale}] {v}" for i, (k, v) in enumerate(fields.items()) if i % 2 == 0},
            input_tokens=10,
            output_tokens=20,
            model_id="stub",
        ),
    )

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "completed"
    assert "language_warning" in types_of(job)
    _, body = stubs["uploads"][0]
    assert "[es] Cue number 1." in body
    assert "Cue number 2." in body  # untranslated, still present
