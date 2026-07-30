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
    state = {
        "uploads": [], "deleted": [], "usage": [],
        "archived": [], "records": [], "session": FakeSession(),
    }

    monkeypatch.setattr(
        svc,
        "translate_fields",
        lambda fields, locale, rules=None: TranslationOutput(
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
    monkeypatch.setattr(svc.vimeo, "missing_scopes", lambda: [])
    monkeypatch.setattr(
        svc.vimeo,
        "get_video_metadata",
        lambda ref: {
            "name": "FAFSA Walkthrough",
            "created_time": "2026-07-01T10:00:00+00:00",
            "duration": 1745,
        },
    )
    monkeypatch.setattr(
        svc, "archive_transcript",
        lambda vid, label, content: state["archived"].append((label, content)) or f"s3/{label}.vtt",
    )
    monkeypatch.setattr(
        svc, "upsert_record", lambda db, **kw: state["records"].append(kw)
    )
    monkeypatch.setattr(
        svc.vimeo,
        "resolve_language",
        lambda locale, name: {"es": ("es", "Spanish"), "zh": ("zh-Hans", "Chinese")}[locale],
    )
    monkeypatch.setattr(svc.vimeo, "list_text_tracks", lambda ref, **kw: [])
    monkeypatch.setattr(
        svc.vimeo,
        "download_source_track",
        lambda ref, lang="en": (VTT, "Vimeo AI captions.vtt"),
    )
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
        lambda ref, **kw: [
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
    def boom(fields, locale, rules=None):
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

    monkeypatch.setattr(svc.vimeo, "get_video_metadata", missing)

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

    monkeypatch.setattr(svc.vimeo, "get_video_metadata", explode)

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))  # must not raise

    assert job.status == "failed"
    assert job.events[-1]["type"] == "error"


def test_cues_the_model_merged_away_are_recovered_by_the_backfill(stubs, monkeypatch):
    """The model drops cues on multi-cue batches but handles them one at a time.

    Mirrors the real failure: mid-sentence fragments get reflowed into a
    neighbouring key, so the source key is absent from the response.
    """
    monkeypatch.setattr(
        svc,
        "translate_fields",
        # Drops every other key when given a batch; a single-key request (the
        # backfill) always succeeds — exactly the observed model behaviour.
        lambda fields, locale, rules=None: TranslationOutput(
            fields={
                k: f"[{locale}] {v}"
                for i, (k, v) in enumerate(fields.items())
                if len(fields) == 1 or i % 2 == 0
            },
            input_tokens=10,
            output_tokens=20,
            model_id="stub",
        ),
    )

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "completed"
    # Backfill rescued them all, so nothing is reported as left in English.
    assert "language_warning" not in types_of(job)
    body = stubs["uploads"][0][1]
    assert "[es] Cue number 1." in body
    assert "[es] Cue number 2." in body


def test_cues_that_fail_even_the_backfill_keep_english(stubs, monkeypatch):
    """Last line of defence: still a valid track, just partly untranslated."""
    monkeypatch.setattr(
        svc,
        "translate_fields",
        # Never returns key "1", batched or alone.
        lambda fields, locale, rules=None: TranslationOutput(
            fields={k: f"[{locale}] {v}" for k, v in fields.items() if k != "1"},
            input_tokens=10,
            output_tokens=20,
            model_id="stub",
        ),
    )

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "completed"
    warning = next(e for e in job.events if e["type"] == "language_warning")
    assert warning["missing_cues"] == 1
    body = stubs["uploads"][0][1]
    assert "Cue number 2." in body  # cue index 1 → source text preserved


# ── Vimeo-sourced captions (no uploaded file) ─────────────────────────────────


def test_uses_the_videos_own_track_when_no_file_is_uploaded(stubs):
    """vtt_content=None means "translate what's already on the video"."""
    job = create_job("123")
    asyncio.run(svc.run_job(job, None, ["es"]))

    assert job.status == "completed"
    source = next(e for e in job.events if e["type"] == "source")
    assert source["origin"] == "vimeo"
    assert source["name"] == "Vimeo AI captions.vtt"
    assert "[es] Cue number 1." in stubs["uploads"][0][1]


def test_uploaded_file_takes_precedence_over_the_vimeo_track(stubs, monkeypatch):
    monkeypatch.setattr(
        svc.vimeo,
        "download_source_track",
        lambda ref, lang="en": ("WEBVTT\n\n00:00:00.000 --> 00:00:01.000\nWRONG SOURCE\n", "x"),
    )
    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    source = next(e for e in job.events if e["type"] == "source")
    assert source["origin"] == "upload"
    assert "WRONG SOURCE" not in stubs["uploads"][0][1]


def test_video_without_a_source_track_fails_before_any_spend(stubs, monkeypatch):
    def no_track(ref, lang="en"):
        raise VimeoError(f"This video has no {lang} caption track to translate from.")

    monkeypatch.setattr(svc.vimeo, "download_source_track", no_track)

    job = create_job("123")
    asyncio.run(svc.run_job(job, None, ["es"]))

    assert job.status == "failed"
    assert job.events[-1]["type"] == "error"
    assert "no en caption track" in job.events[-1]["error"]
    assert not stubs["usage"] and not stubs["uploads"]


# ── Chunk-level resilience ────────────────────────────────────────────────────


def test_transient_chunk_failure_is_retried(stubs, monkeypatch):
    """Haiku returns malformed JSON non-deterministically; a retry usually works."""
    calls = {"n": 0}

    def flaky(fields, locale, rules=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise TranslationError("Bedrock response was not valid JSON")
        return TranslationOutput(
            fields={k: f"[{locale}] {v}" for k, v in fields.items()},
            input_tokens=10,
            output_tokens=20,
            model_id="stub",
        )

    monkeypatch.setattr(svc, "translate_fields", flaky)

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "completed"
    assert calls["n"] == 2, "the failed chunk should be retried once"
    assert "[es] Cue number 1." in stubs["uploads"][0][1]


def test_one_permanently_bad_chunk_still_publishes_the_rest(stubs, monkeypatch):
    """A chunk that never parses leaves its cues in English, not the whole track."""
    # Force several chunks so one can fail while others succeed.
    monkeypatch.setattr(svc, "_CUE_MAX_COUNT", 3)

    def selective(fields, locale, rules=None):
        if "0" in fields:  # always fail the chunk containing the first cue
            raise TranslationError("Bedrock response was not valid JSON")
        return TranslationOutput(
            fields={k: f"[{locale}] {v}" for k, v in fields.items()},
            input_tokens=10,
            output_tokens=20,
            model_id="stub",
        )

    monkeypatch.setattr(svc, "translate_fields", selective)

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "completed"
    assert "language_warning" in types_of(job)
    _, body = stubs["uploads"][0]
    assert "Cue number 1." in body          # untranslated, preserved
    assert "[es] Cue number 12." in body    # other chunks translated


def test_every_chunk_failing_reports_an_error_instead_of_english_captions(stubs, monkeypatch):
    """Systemic failure must not silently publish an all-English 'translation'."""

    def always_bad(fields, locale, rules=None):
        raise TranslationError("Bedrock response was not valid JSON")

    monkeypatch.setattr(svc, "translate_fields", always_bad)

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "failed"
    assert "language_error" in types_of(job)
    assert not stubs["uploads"]


def test_blank_model_output_counts_as_missing_and_keeps_english(stubs, monkeypatch):
    """A "" value must fall back to source text, not publish an empty caption."""
    monkeypatch.setattr(
        svc,
        "translate_fields",
        lambda fields, locale, rules=None: TranslationOutput(
            fields={k: ("" if k == "3" else f"[{locale}] {v}") for k, v in fields.items()},
            input_tokens=10,
            output_tokens=20,
            model_id="stub",
        ),
    )

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    warning = next(e for e in job.events if e["type"] == "language_warning")
    assert warning["missing_cues"] == 1
    body = stubs["uploads"][0][1]
    assert "Cue number 4." in body  # cue index 3 → "Cue number 4."
    # No timing line may be left without text.
    import re as _re
    for block in _re.split(r"\n\s*\n", body.strip()):
        if "-->" in block:
            assert block.strip().split("\n")[-1].strip(), f"empty cue: {block!r}"


def test_token_missing_a_scope_fails_before_spending_anything(stubs, monkeypatch):
    """A scope gap used to surface only at upload, after paying for translation."""
    monkeypatch.setattr(svc.vimeo, "missing_scopes", lambda: ["upload", "delete"])

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "failed"
    assert job.events[-1]["type"] == "error"
    assert "upload, delete" in job.events[-1]["error"]
    assert not stubs["usage"] and not stubs["uploads"]


# ── Registry + S3 archival ────────────────────────────────────────────────────


def test_source_and_each_translation_are_archived_to_s3(stubs):
    job = create_job("123:hash")
    asyncio.run(svc.run_job(job, VTT, ["es", "zh"]))

    labels = [label for label, _ in stubs["archived"]]
    assert labels == ["source", "es", "zh"]
    # The archived source is the English input; each locale gets its published VTT.
    source_body = dict(stubs["archived"])["source"]
    assert "Cue number 1." in source_body and "[es]" not in source_body
    assert "[es] Cue number 1." in dict(stubs["archived"])["es"]


def test_registry_row_captures_vimeo_metadata_and_results(stubs):
    job = create_job("123:hash")
    asyncio.run(svc.run_job(job, VTT, ["es", "zh"]))

    assert len(stubs["records"]) == 1
    rec = stubs["records"][0]
    assert rec["video_ref"] == "123:hash"
    assert rec["metadata"]["name"] == "FAFSA Walkthrough"
    assert rec["metadata"]["duration"] == 1745
    assert rec["source_origin"] == "upload"
    assert rec["source_cue_count"] == 12
    assert rec["source_s3_key"] == "s3/source.vtt"
    assert rec["status"] == "completed"
    assert set(rec["translations"]) == {"es", "zh"}
    assert rec["translations"]["zh"]["vimeo_code"] == "zh-Hans"
    assert rec["translations"]["es"]["s3_key"] == "s3/es.vtt"
    assert rec["translations"]["es"]["missing_cues"] == 0


def test_vimeo_sourced_run_is_recorded_with_that_origin(stubs):
    job = create_job("123")
    asyncio.run(svc.run_job(job, None, ["es"]))

    rec = stubs["records"][0]
    assert rec["source_origin"] == "vimeo"
    assert rec["source_name"] == "Vimeo AI captions.vtt"


def test_a_failed_run_is_still_recorded(stubs, monkeypatch):
    """The attempt must show in the admin list, not vanish."""
    def boom(fields, locale, rules=None):
        raise TranslationError("Bedrock throttled (429)")

    monkeypatch.setattr(svc, "translate_fields", boom)

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert stubs["records"][0]["status"] == "failed"
    assert stubs["records"][0]["translations"] == {}


def test_archival_failure_does_not_fail_a_published_job(stubs, monkeypatch):
    """S3 is bookkeeping — captions are already live on Vimeo by then."""
    monkeypatch.setattr(svc, "archive_transcript", lambda vid, label, content: None)

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "completed"
    assert stubs["records"][0]["translations"]["es"]["s3_key"] is None


def test_registry_failure_does_not_fail_a_published_job(stubs, monkeypatch):
    def boom(db, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr(svc, "upsert_record", boom)

    job = create_job("123")
    asyncio.run(svc.run_job(job, VTT, ["es"]))

    assert job.status == "completed"
    assert job.events[-1]["succeeded"] == ["es"]
