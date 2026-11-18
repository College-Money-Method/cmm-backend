"""Grade one counselor-assistant answer against its eval case.

Two layers: deterministic checks that need no model (citation numbers, links,
money amounts and contact details that are not in the prompt, retrieval hits),
and a Sonnet judge that reads the case's rubric, the sources and the answer and
returns JSON. A case passes only when both layers pass.
"""

from __future__ import annotations

import json
import re

from eval_style import style_issues

from src.config import settings
from src.video_pipeline import bedrock_usage
from src.video_pipeline.bedrock_client import get_client

_CITATION = re.compile(r"\[(\d{1,2})\]")
_URL = re.compile(r"https?://[^\s)\]>\"']+")
_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE = re.compile(r"\(?\b\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b|1-800-[\w-]+")
JUDGE_MAX_TOKENS = 900


def deterministic_checks(case: dict, answer: str, system_prompt: str, chunks: list) -> dict:
    """Facts the answer may not invent, checked against the exact prompt it was given."""
    cited = sorted({int(n) for n in _CITATION.findall(answer)})
    def unknown(pattern: re.Pattern) -> list[str]:
        return sorted(_found(pattern, answer) - _found(pattern, system_prompt))

    titles = " | ".join(f"{c.title} {c.heading or ''}" for c in chunks).lower()
    hints = case.get("source_hints") or []
    return {
        "citations": cited,
        "bad_citations": [n for n in cited if n < 1 or n > len(chunks)],
        "invented_urls": unknown(_URL),
        "invented_money": unknown(_MONEY),
        "invented_contacts": unknown(_EMAIL) + unknown(_PHONE),
        "asks_question": "?" in answer,
        "retrieval_hit": any(h.lower() in titles for h in hints) if hints else None,
        "style": style_issues(answer),
    }


def _found(pattern: re.Pattern, text: str) -> set[str]:
    """Matches normalized so formatting alone never counts as invented ($5,000 == $5000, trailing ``/`` or ``**``)."""
    if pattern is _PHONE:
        return {re.sub(r"\D", "", m) or m for m in pattern.findall(text)}
    return {re.sub(r"[,\s]", "", m).rstrip(".;:!?*/") for m in pattern.findall(text)}


def deterministic_ok(checks: dict) -> bool:
    return not (checks["bad_citations"] or checks["invented_urls"] or checks["invented_money"]
                or checks["invented_contacts"])


_JUDGE_SYSTEM = """You grade answers from an AI assistant for high school counselors run by College Money Method (CMM), a college financial aid education company. The assistant must answer only from the numbered sources and school data it was given.

Modes the case expects:
- answer: give the standing answer, citing sources. A short follow-up question is fine but must not replace the answer.
- clarify: the right answer depends on a detail the counselor has not given. Pass when the assistant asks for at least one of the listed details (it may also give a general or conditional answer). Mark a must_ask item asked (true) when the answer asks for that detail in any wording, or for a narrower detail that decides the same thing, or when the counselor's message already states it. mode_followed is true for clarify when the answer asks for at least one detail that is still missing; the listed details are the usual ones, not a script.
- answer_route: answer the part the sources cover, then name who should handle the rest.
- route: hand off to the named destination; share standing information only when its sources give it.

The team guidance section of the prompt is written by CMM staff and counts as a source: a claim it supports is grounded, and routing it names is correct.

When a case is marked needs_policy, CMM has not yet written the policy facts. Then the right behavior is to say plainly that it does not have that information and route to the destination, without guessing; do not fail it for missing facts it was never given. If the team guidance already gives a fact, answering from it is correct; mark a must_cover item covered when the answer either states it from the prompt or says plainly it does not have it and routes it.

Reply with JSON only, no prose around it:
{"mode_followed": true|false,
 "asked": [true|false per must_ask item, same order],
 "covered": [true|false per must_cover item, same order],
 "routed": true|false|null (null when the case has no route_to),
 "violations": [the must_not items the answer commits, verbatim],
 "grounded": true|false (false if any factual claim is not supported by the sources or school data),
 "score": 1-5 (5 = a counselor could use it as is),
 "voice": 1-5 (CMM's voice, graded apart from content: 5 = warm, direct, plain everyday words, short paragraphs, honest, with the partnership "we" and a clear next step in anything written to families; 1 = corporate, jargon-heavy, hyped or cold),
 "notes": "one sentence on the biggest problem, or what made it good"}"""


def judge(case: dict, answer: str, system_prompt: str) -> tuple[dict, tuple[int, int]]:
    """Sonnet's rubric verdict and the tokens it billed."""
    rubric = {k: case.get(k) for k in ("mode", "must_ask", "must_cover", "route_to", "must_not", "needs_policy")}
    user = (
        f"CASE RUBRIC:\n{json.dumps(rubric, indent=1)}\n\n"
        f"WHAT THE ASSISTANT WAS GIVEN (its system prompt, with sources):\n<prompt>\n{system_prompt}\n</prompt>\n\n"
        f"COUNSELOR MESSAGE:\n{case['message']}\n\nASSISTANT ANSWER:\n<answer>\n{answer}\n</answer>"
    )
    model = settings.bedrock_sonnet_model_id
    response = get_client().messages.create(
        model=model, max_tokens=JUDGE_MAX_TOKENS, system=_JUDGE_SYSTEM,
        messages=[{"role": "user", "content": user}],
    )
    tokens = (response.usage.input_tokens or 0, response.usage.output_tokens or 0)
    bedrock_usage.record(bedrock_usage.ASSISTANT_EVAL, model, *tokens)
    text = "".join(block.text for block in response.content if block.type == "text")
    return parse_verdict(text), tokens


def parse_verdict(text: str) -> dict:
    """The judge's JSON object, tolerating a fenced block; an unreadable reply fails the case."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        return json.loads(match.group(0)) if match else {"error": "no json"}
    except json.JSONDecodeError:
        return {"error": "bad json"}


def verdict_ok(case: dict, verdict: dict) -> bool:
    if "error" in verdict:
        return False
    covered = verdict.get("covered") or []
    if len(covered) != len(case.get("must_cover") or []):
        return False
    coverage_ok = not covered or sum(map(bool, covered)) * 2 >= len(covered)
    asked = verdict.get("asked") or []
    asked_ok = case["mode"] != "clarify" or any(asked)
    return bool(
        verdict.get("mode_followed") and verdict.get("grounded") and not verdict.get("violations")
        and verdict.get("routed") is not False and coverage_ok and asked_ok
    )
