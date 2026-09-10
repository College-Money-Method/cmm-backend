"""Getting an answer out of a reply that is not only the answer.

Asking for JSON and nothing else does not always get it. On a real 262-frame
run the classifier answered correctly and then explained itself in a paragraph,
and a whole-string parse rejected both — two frames were lost outright and eight
more cost a second call. The object is at the front either way, so that is what
is read.
"""

from __future__ import annotations

import json

import pytest

from src.video_pipeline.bedrock_client import parse_leading_object, strip_code_fences


def test_a_bare_object_is_read():
    assert parse_leading_object('{"type": "speaker", "heading": ""}') == {
        "type": "speaker",
        "heading": "",
    }


def test_a_paragraph_after_the_object_is_ignored():
    """Verbatim from the run: the answer was right, the explanation was extra."""
    reply = (
        '{"type": "content_slide", "heading": ""}\n\n'
        "This is a content slide because it contains a heading along with a "
        "presenter bio, affiliations, and a photograph."
    )

    assert parse_leading_object(reply)["type"] == "content_slide"


def test_a_fenced_object_with_a_paragraph_after_it_is_read():
    """Both habits at once, which is how it actually arrived."""
    reply = (
        '```json\n{"type": "title_card", "heading": "The Aid Formula"}\n```\n\n'
        "It is a single heading on a plain background."
    )

    assert parse_leading_object(strip_code_fences(reply)) == {
        "type": "title_card",
        "heading": "The Aid Formula",
    }


def test_prose_before_the_object_is_still_an_error():
    """Only trailing text is tolerated. With the answer buried mid-sentence there
    is no way to know which object is the answer."""
    with pytest.raises(json.JSONDecodeError):
        parse_leading_object('Here is my answer: {"type": "speaker"}')


def test_a_reply_that_is_not_an_object_is_an_error():
    with pytest.raises(json.JSONDecodeError):
        parse_leading_object('["speaker"]')
