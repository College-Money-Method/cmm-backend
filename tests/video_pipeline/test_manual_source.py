"""What the one paste field on the admin form accepts.

This is the whole input surface of a manual run, so its job is to be specific
about a bad paste. Every rejection below has to say enough that the operator
knows what to paste instead — a run that fails inside an ECS task ten minutes
later costs a download and tells them nothing.
"""

from __future__ import annotations

import pytest

from src.video_pipeline.manual_source import URL, ZOOM, SourceError, parse_source


def test_a_bare_meeting_id_is_a_zoom_source():
    source = parse_source("88812345678")

    assert source.kind == ZOOM
    assert source.is_zoom is True
    assert source.zoom_reference == "88812345678"


@pytest.mark.parametrize("pasted", ["881 2345 6789", "881-2345-6789", "  8812345678  "])
def test_the_spacing_zooms_own_ui_shows_is_accepted(pasted):
    """Zoom displays "881 2345 6789"; asking an operator to strip that is a
    rejection they would read as the id being wrong."""
    assert parse_source(pasted).zoom_reference.isdigit()


def test_a_recording_uuid_is_a_zoom_source():
    source = parse_source("abcdEFGH1234+/==xyz")

    assert source.kind == ZOOM
    assert source.zoom_reference == "abcdEFGH1234+/==xyz"


def test_a_leading_slash_in_a_recording_uuid_survives():
    """A UUID beginning with "/" is legitimate and is the reason the Zoom client
    double-encodes it. Stripping or rejecting it here would lose real sources."""
    assert parse_source("/abcdEFGH1234567890==").zoom_reference.startswith("/")


@pytest.mark.parametrize(
    "link",
    [
        "https://us02web.zoom.us/j/88812345678",
        "https://us02web.zoom.us/w/88812345678?tk=abc",
        "https://zoom.us/webinar/88812345678",
    ],
)
def test_a_zoom_link_yields_the_id_inside_it(link):
    source = parse_source(link)

    assert source.kind == ZOOM
    assert source.zoom_reference == "88812345678"


def test_a_share_link_is_refused_with_what_to_paste_instead():
    """A /rec/share/ link is a per-share token no API resolves, so accepting it
    would promise a run that cannot start."""
    with pytest.raises(SourceError, match="recording UUID instead"):
        parse_source("https://us02web.zoom.us/rec/share/AbCdEf_gHiJkLmNoP")


def test_a_zoom_url_with_no_id_in_it_is_refused():
    with pytest.raises(SourceError, match="No Zoom meeting or webinar ID"):
        parse_source("https://us02web.zoom.us/my/recordings")


def test_a_download_url_is_a_url_source():
    url = "https://cmm-media.s3.us-east-1.amazonaws.com/raw/session.mp4?X-Amz-Signature=x"
    source = parse_source(url)

    assert source.kind == URL
    assert source.is_zoom is False
    assert source.url == url


def test_the_query_string_of_a_presigned_url_is_kept_verbatim():
    """Dropping it would strip the signature and every fetch would 403."""
    url = "https://b.s3.amazonaws.com/a.mp4?X-Amz-Expires=900&X-Amz-Signature=deadbeef"
    assert parse_source(url).url == url


def test_http_is_refused():
    with pytest.raises(SourceError, match="Only https"):
        parse_source("http://example.com/a.mp4")


def test_a_non_http_scheme_is_refused():
    with pytest.raises(SourceError, match="Only https"):
        parse_source("s3://cmm-media/raw/session.mp4")


def test_an_empty_paste_says_what_the_field_takes():
    with pytest.raises(SourceError, match="Zoom meeting ID"):
        parse_source("   ")


def test_a_short_number_is_not_a_meeting_id():
    with pytest.raises(SourceError, match="9-11 digits"):
        parse_source("12345")


def test_prose_is_refused():
    with pytest.raises(SourceError):
        parse_source("the one from last Tuesday")
