"""Transcript download + direct track publish.

Covers the download → edit → re-publish loop an admin uses to correct a line
without re-translating, and the guardrail that download keys come from the
registry rather than from the caller.
"""

import pytest

import src.db.models  # noqa: F401 — instantiating an ORM model configures mappers,
# which needs the full model graph registered (relationships resolve by name).
from src.content import video_caption_archive as archive
from src.content import video_cc_service as svc
from src.content.video_caption_models import VideoCaptionRecord
from src.content.vtt_parser import VttError
from src.integrations.vimeo import VimeoError

VTT = (
    "WEBVTT\n\n1\n00:00:01.000 --> 00:00:02.000\nHola.\n\n"
    "2\n00:00:02.000 --> 00:00:04.000\nBuenos días.\n"
)


@pytest.fixture
def record():
    return VideoCaptionRecord(
        video_id="123",
        privacy_hash="hash",
        title="A Video",
        source_s3_key="video-cc/123/source-aaa.vtt",
        translations={
            "es": {"language": "Spanish", "s3_key": "video-cc/123/es-bbb.vtt"},
            "zh": {"language": "Chinese"},  # older run, never archived
        },
    )


def test_key_resolution_covers_source_and_locales(record):
    assert archive.resolve_transcript_key(record, "source") == "video-cc/123/source-aaa.vtt"
    assert archive.resolve_transcript_key(record, "es") == "video-cc/123/es-bbb.vtt"


@pytest.mark.parametrize("label", ["zh", "fr", "../../etc/passwd", ""])
def test_unknown_or_unarchived_labels_resolve_to_nothing(record, label):
    """Keys come from the record, so a crafted label cannot reach other objects."""
    assert archive.resolve_transcript_key(record, label) is None


def test_presign_failure_returns_none_rather_than_raising(monkeypatch):
    class Boom:
        def generate_presigned_url(self, *a, **kw):
            raise RuntimeError("s3 down")

    monkeypatch.setattr(archive, "get_s3_client", lambda: Boom())
    assert archive.presign_transcript("k", "f.vtt") is None


# ── Direct publish (no translation) ───────────────────────────────────────────


@pytest.fixture
def publish_stubs(monkeypatch):
    state = {"uploaded": [], "deleted": [], "archived": [], "records": [], "translated": 0}

    monkeypatch.setattr(svc.vimeo, "missing_scopes", lambda: [])
    monkeypatch.setattr(
        svc.vimeo, "get_video_metadata",
        lambda ref: {"name": "A Video", "created_time": None, "duration": 300},
    )
    monkeypatch.setattr(svc.vimeo, "resolve_language", lambda loc, name: ("es", "Spanish"))
    monkeypatch.setattr(svc.vimeo, "list_text_tracks", lambda ref, **kw: [
        {"uri": "/videos/123/texttracks/old", "language": "es"}
    ])
    monkeypatch.setattr(svc.vimeo, "delete_text_track", lambda uri: state["deleted"].append(uri))
    monkeypatch.setattr(
        svc.vimeo, "upload_text_track",
        lambda ref, lang, name, content, active=True: (
            state["uploaded"].append((lang, content)) or "/videos/123/texttracks/new"
        ),
    )
    monkeypatch.setattr(svc, "archive_transcript", lambda v, l, c: f"s3/{l}.vtt")
    monkeypatch.setattr(svc, "get_record", lambda db, vid: None)
    monkeypatch.setattr(svc, "upsert_record", lambda db, **kw: state["records"].append(kw))
    # If this ever fires, the edited file was wrongly re-translated.
    monkeypatch.setattr(
        svc, "translate_fields",
        lambda *a, **kw: state.__setitem__("translated", state["translated"] + 1),
    )
    return state


class FakeSession:
    def add(self, x): pass
    def commit(self): pass
    def rollback(self): pass
    def close(self): pass


def test_edited_track_is_published_verbatim_without_translation(publish_stubs):
    result = svc.publish_edited_track(FakeSession(), "123:hash", "es", VTT)

    assert publish_stubs["translated"] == 0, "an edited track must never be re-translated"
    lang, body = publish_stubs["uploaded"][0]
    assert lang == "es"
    assert body == VTT, "the file must reach Vimeo byte-for-byte"
    assert result["cue_count"] == 2
    assert result["edited"] is True


def test_publishing_replaces_the_existing_track_for_that_language(publish_stubs):
    svc.publish_edited_track(FakeSession(), "123:hash", "es", VTT)
    assert publish_stubs["deleted"] == ["/videos/123/texttracks/old"]


def test_publish_records_the_result_in_the_registry(publish_stubs):
    svc.publish_edited_track(FakeSession(), "123:hash", "es", VTT)
    rec = publish_stubs["records"][0]
    assert rec["status"] == "completed"
    assert rec["translations"]["es"]["s3_key"] == "s3/es.vtt"


def test_publish_rejects_a_file_that_is_not_captions(publish_stubs):
    with pytest.raises(VttError):
        svc.publish_edited_track(FakeSession(), "123:hash", "es", "not a caption file")
    assert not publish_stubs["uploaded"]


def test_publish_checks_token_scopes_first(publish_stubs, monkeypatch):
    monkeypatch.setattr(svc.vimeo, "missing_scopes", lambda: ["delete"])
    with pytest.raises(VimeoError, match="delete"):
        svc.publish_edited_track(FakeSession(), "123:hash", "es", VTT)
    assert not publish_stubs["uploaded"]
