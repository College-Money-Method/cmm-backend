"""Run the counselor-assistant eval: retrieve, answer and grade every case for one school.

Each case goes through the same retrieval scope and system prompt as the live
counselor chat (``counselor_prompt_builder``), gets one Haiku answer, and is
graded by ``eval_grading``. Nothing is saved to conversations; Bedrock spend is
recorded as ``assistant_eval`` (the question embeddings too).

Usage (from the repo root):
    uv run python scripts/assistant_eval/run_counselor_eval.py --school annie-wright-schools
    uv run python scripts/assistant_eval/run_counselor_eval.py --only 03,13,r2 --no-judge
    uv run python scripts/assistant_eval/run_counselor_eval.py --guidance-file scripts/assistant_eval/counselor_playbooks.json

``--guidance-file`` adds draft team-guidance sections (JSON list of
``{"title", "body"}``) after the database's own (replacing any with the same title), so a playbook can be graded
before anyone saves it in Admin -> Assistant.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_grading import deterministic_checks, deterministic_ok, judge, verdict_ok
from eval_report import summary_markdown
from sqlalchemy import select

import src.db.models  # noqa: F401 - registers every mapper before the first query
from src.assistant.answer import MAX_ANSWER_TOKENS
from src.assistant.chat_turn import school_links
from src.assistant.counselor_context import COUNSELOR_AUDIENCE, counselor_prompt_builder
from src.assistant.embeddings import embed_query
from src.assistant.family_school_scope import scope_for_school
from src.assistant.prompt_sections import format_guidance, guidance_parts
from src.assistant.search import hybrid_search
from src.config import settings
from src.db.base import get_session_factory
from src.schools.models import School
from src.video_pipeline import bedrock_usage
from src.video_pipeline.bedrock_client import get_client

CASES_PATH = Path(__file__).resolve().parent / "counselor_cases.json"
OUTPUT_DIR = PROJECT_ROOT / "scripts" / "output" / "assistant_eval"


def load_cases(path: Path, only: str | None) -> list[dict]:
    """Every case, or those whose id starts with one of the comma-separated ``only`` prefixes."""
    cases = json.loads(path.read_text())["cases"]
    if not only:
        return cases
    prefixes = tuple(p.strip().lower() for p in only.split(",") if p.strip())
    return [c for c in cases if c["id"].startswith(prefixes)]


def prepare(db, school: School, cases: list[dict], guidance: str) -> list[dict]:
    """Retrieval and prompt for each case, sequentially (one DB session)."""
    scope = scope_for_school(school, COUNSELOR_AUDIENCE, "full")
    prepared = []
    for case in cases:
        # Per case: the message picks the workshop template for a workshop email.
        build_prompt = counselor_prompt_builder(db, school, None, guidance=guidance, message=case["message"])
        vector = embed_query(case["message"], bedrock_usage.ASSISTANT_EVAL)
        chunks = school_links(hybrid_search(db, case["message"], vector, scope), school.slug)
        prepared.append({"case": case, "chunks": chunks, "system": build_prompt(chunks)})
    return prepared


def answer(system_prompt: str, message: str) -> tuple[str, tuple[int, int]]:
    model = settings.assistant_answer_model_id
    response = get_client().messages.create(
        model=model, max_tokens=MAX_ANSWER_TOKENS, system=system_prompt,
        messages=[{"role": "user", "content": message}],
    )
    tokens = (response.usage.input_tokens or 0, response.usage.output_tokens or 0)
    bedrock_usage.record(bedrock_usage.ASSISTANT_EVAL, model, *tokens)
    return "".join(b.text for b in response.content if b.type == "text").strip(), tokens


def run_case(item: dict, use_judge: bool) -> dict:
    case, chunks, system = item["case"], item["chunks"], item["system"]
    started = time.monotonic()
    try:
        text, answer_tokens = answer(system, case["message"])
    except Exception as exc:  # noqa: BLE001 - one failed call must not end the run
        return {"id": case["id"], "error": f"answer failed: {exc}", "passed": False}
    checks = deterministic_checks(case, text, system, chunks)
    try:
        verdict, judge_tokens = judge(case, text, system) if use_judge else ({}, (0, 0))
    except Exception as exc:  # noqa: BLE001 - keep the paid answer; the case fails on the judge error
        verdict, judge_tokens = {"error": f"judge failed: {exc}"}, (0, 0)
    passed = deterministic_ok(checks) and (verdict_ok(case, verdict) if use_judge else True)
    return {
        "id": case["id"], "theme": case["theme"], "theme_name": case["theme_name"], "mode": case["mode"],
        "variant": case.get("variant", "base"), "needs_policy": case.get("needs_policy", False),
        "message": case["message"], "answer": text, "passed": passed, "checks": checks, "verdict": verdict,
        "sources": [f"{c.source_type}: {c.title}" + (f" — {c.heading}" if c.heading else "") for c in chunks],
        "tokens": {"answer": answer_tokens, "judge": judge_tokens}, "seconds": round(time.monotonic() - started, 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--school", default="annie-wright-schools", help="school slug to answer as")
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--only", help="comma-separated case id prefixes, e.g. 03,13,r2")
    parser.add_argument("--guidance-file", type=Path, help="draft guidance sections to add (JSON list)")
    parser.add_argument("--no-judge", action="store_true", help="deterministic checks only")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--label", default="", help="added to the output file names")
    args = parser.parse_args()

    cases = load_cases(args.cases, args.only)
    with get_session_factory()() as db:
        school = db.scalars(select(School).where(School.slug == args.school)).first()
        if school is None:
            sys.exit(f"No school with slug {args.school!r}")
        parts = guidance_parts(db, COUNSELOR_AUDIENCE)
        if args.guidance_file:
            # A draft replaces a saved section with the same title, so a seeded playbook is not doubled.
            drafts = [(s["title"].strip(), s["body"]) for s in json.loads(args.guidance_file.read_text())
                      if s.get("audience", COUNSELOR_AUDIENCE) in (COUNSELOR_AUDIENCE, "both")]
            titles = {t.lower() for t, _ in drafts}
            parts = [p for p in parts if p[0].strip().lower() not in titles] + drafts
        prepared = prepare(db, school, cases, format_guidance(parts))
        school_name = school.name

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda item: run_case(item, not args.no_judge), prepared))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = OUTPUT_DIR / f"counselor-{stamp}{'-' + args.label if args.label else ''}"
    meta = {"school": school_name, "cases": len(results), "judge": not args.no_judge,
            "guidance_file": str(args.guidance_file or ""), "model": settings.assistant_answer_model_id,
            "run_at": stamp}
    base.with_suffix(".json").write_text(json.dumps({"meta": meta, "results": results}, indent=1))
    report = summary_markdown(meta, results)
    base.with_suffix(".md").write_text(report)
    print(report)
    print(f"\nWrote {base.with_suffix('.json').relative_to(PROJECT_ROOT)} and .md")


if __name__ == "__main__":
    main()
