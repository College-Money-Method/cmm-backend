"""Repair of malformed Bedrock JSON responses.

Captions quoting UI labels ("I can't find my school") come back with unescaped
double quotes inside JSON string values, which invalidates an entire batch and
throws away ~25 otherwise-good translations. Repair runs only after json.loads
has already failed, so it can never make a valid response worse.
"""

import json

import pytest

from src.content.bedrock_translation import _repair_flat_json_object

# Verbatim shape of a real failing response.
MALFORMED = """{
  "153": "您可以通过链接手动输入",
  "154": "到"我找不到我的学校"。",
  "166": "点击"添加更多学校"以添加其他大学。",
  "167": "在初始 FAFSA 申请中，"
}"""


def test_unescaped_inner_quotes_are_recovered():
    with pytest.raises(json.JSONDecodeError):
        json.loads(MALFORMED)

    parsed = json.loads(_repair_flat_json_object(MALFORMED))
    assert parsed["154"] == '到"我找不到我的学校"。'
    assert parsed["166"] == '点击"添加更多学校"以添加其他大学。'
    assert len(parsed) == 4


def test_already_escaped_values_are_not_double_escaped():
    valid = '{\n  "1": "He said \\"hi\\" today"\n}'
    assert json.loads(_repair_flat_json_object(valid))["1"] == 'He said "hi" today'


def test_values_without_quotes_are_untouched():
    plain = '{\n  "1": "Hola, buenos días",\n  "2": "你好"\n}'
    assert json.loads(_repair_flat_json_object(plain)) == json.loads(plain)


@pytest.mark.parametrize(
    "text",
    [
        "[1, 2, 3]",                       # not an object
        '{"a": {"nested": "object"}}',     # not one-pair-per-line
        "totally unstructured text",
    ],
)
def test_unhandled_shapes_return_none(text):
    """Declining is required — the caller must fall through to its error path."""
    assert _repair_flat_json_object(text) is None


def test_trailing_comma_and_final_line_are_preserved():
    repaired = _repair_flat_json_object(MALFORMED)
    assert repaired.rstrip().endswith("}")
    assert '"167"' in repaired


# Verbatim single-line payload captured from a live run — the layout the
# original line-based repair silently declined, forcing three costly retries.
COMPACT = (
    '{"62": "当您登录FAFSA网站时，第一个屏幕", "63": "会询问您是谁，", '
    '"66": "我将选择"我是学生"", "67": "并想要访问FAFSA表格。"}'
)


def test_compact_single_line_object_is_repaired():
    with pytest.raises(json.JSONDecodeError):
        json.loads(COMPACT)

    parsed = json.loads(_repair_flat_json_object(COMPACT))
    assert parsed["66"] == '我将选择"我是学生"'
    assert len(parsed) == 4


def test_multiline_and_compact_layouts_agree():
    """Same content, two layouts — repair must produce the same mapping."""
    multiline = json.loads(_repair_flat_json_object(MALFORMED))
    same_compact = "{" + ", ".join(
        f'"{k}": "{v}"' for k, v in [
            ("153", "您可以通过链接手动输入"),
            ("154", '到"我找不到我的学校"。'),
            ("166", '点击"添加更多学校"以添加其他大学。'),
            ("167", "在初始 FAFSA 申请中，"),
        ]
    ) + "}"
    assert json.loads(_repair_flat_json_object(same_compact)) == multiline


def test_embedded_newlines_survive_the_round_trip():
    """Two-line cues carry a newline that must not be mangled."""
    text = '{"1": "first line\\nsecond line", "2": "plain"}'
    parsed = json.loads(_repair_flat_json_object(text))
    assert parsed["1"] == "first line\nsecond line"
