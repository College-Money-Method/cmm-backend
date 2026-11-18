"""The Counselor Hub guide must reach the index as page- and task-sized chunks.

The indexer splits a resource only at Tiptap heading nodes and reads table
cells one per line, so the converter's job is real headings named after their
page and tables turned into labelled lines.
"""

from __future__ import annotations

import json

from scripts.seed.counselor_guide_markdown import INTRO_HEADING, markdown_to_tiptap
from src.assistant.chunking import rich_text_sections

GUIDE = """# Counselor Hub User Guide

The Counselor Hub is where counselors run CMM.
Each account is linked to one school.

## Page index

| Page | Sidebar label | Who can open it |
|---|---|---|
| Home | Home | All roles |

---

## Workshops

- Sidebar label: **Workshops**
- See [Change grade labels](#task-change-grade-labels).

### Workshop card contents

| Part of card | Meaning |
|---|---|
| **Suggested Grades** | The grades the workshop is meant for |

### Task: Change grade labels

1. Tick the grades you want.
2. Click **Save**. The path is `/hub/workshops`.
"""


def sections():
    return {s.heading: s.text for s in rich_text_sections(json.dumps(markdown_to_tiptap(GUIDE)))}


def test_headings_name_their_page_and_task():
    assert list(sections()) == [
        INTRO_HEADING,
        "Page index",
        "Workshops",
        "Workshops: Workshop card contents",
        "Workshops: Change grade labels",
    ]


def test_intro_lines_join_into_one_paragraph():
    assert sections()[INTRO_HEADING] == (
        "The Counselor Hub is where counselors run CMM. Each account is linked to one school."
    )


def test_tables_become_labelled_lines():
    assert sections()["Page index"] == "Home: Sidebar label: Home; Who can open it: All roles"
    assert sections()["Workshops: Workshop card contents"] == (
        "Suggested Grades: The grades the workshop is meant for"
    )


def test_lists_keep_text_and_drop_link_and_code_syntax():
    assert sections()["Workshops"] == "Sidebar label: Workshops\nSee Change grade labels."
    assert sections()["Workshops: Change grade labels"] == (
        "Tick the grades you want.\nClick Save. The path is /hub/workshops."
    )


def test_bold_ui_labels_stay_bold_and_title_and_rules_are_dropped():
    doc = markdown_to_tiptap(GUIDE)
    text = json.dumps(doc)
    assert '"marks": [{"type": "bold"}]' in text
    assert "Counselor Hub User Guide" not in text
    assert "---" not in text
    assert {node["type"] for node in doc["content"]} <= {"heading", "paragraph", "bulletList", "orderedList"}
