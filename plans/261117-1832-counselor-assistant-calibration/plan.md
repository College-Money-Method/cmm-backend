# Counselor assistant calibration (Phase 1 top-20 questions)

**Status:** tooling done · playbooks drafted (not seeded) · waiting on CMM content
**Source:** `AI_Companion_Phase1_Top20_Questions.pdf` (20 themes + 5 routing themes)

## Outcome
The counselor assistant answers the Phase 1 questions the way CMM wants: standing answer with citations,
asks for the deciding detail when one is missing, routes what needs a person (mostly Paul), and never
invents prices, links, contacts or policy.

## What was built
| Piece | Where |
|---|---|
| Shared prompt assembly (live chat + eval use the same code) | `src/assistant/counselor_context.py` |
| Prompt rules: clarify, hand off, no CMM prices | `src/assistant/counselor_prompt.py` |
| 81 eval cases (25 themes × 2–5 phrasings, incl. detail-given variants) | `scripts/assistant_eval/counselor_cases.json` |
| Runner + deterministic checks + Sonnet judge + markdown report | `scripts/assistant_eval/run_counselor_eval.py`, `eval_grading.py`, `eval_report.py` |
| Draft team-guidance playbooks | `scripts/assistant_eval/counselor_playbooks.json` |
| Seed playbooks into Admin → Assistant (dry run default) | `scripts/seed/seed_counselor_assistant_playbooks.py` |
| Content gaps + skeleton for Paul | [content-to-index.md](content-to-index.md) |

## Results (Annie Wright Schools, Haiku 4.5 answers, Sonnet judge)
See [results.md](results.md).

## Remaining steps
1. Paul confirms the playbook facts (routes, partnership scope) → `seed_counselor_assistant_playbooks.py --apply` (dev, then prod).
2. Paul/content team write the counselor program guide + missing aid topics ([content-to-index.md](content-to-index.md)); publish → index sync.
3. Re-run the eval after each content drop; target ≥ 80% overall, 100% on route cases, 0 invented facts.
4. Add real counselor questions from chat logs to `counselor_cases.json` as they come in.

## Acceptance criteria
- [x] Eval reproducible with one command; spend tagged `assistant_eval`
- [x] Live chat prompt unchanged except the new rules (tests pass)
- [ ] Playbooks confirmed and seeded
- [ ] Policy themes (13–20, R1–R5) pass from indexed content, not only playbooks
- [ ] ≥ 80% overall pass

## Unresolved questions
- Is "Paul" the right destination for every route, or are there other owners (billing, Hub support, content team)?
- Should eval runs use a dev DB only (they write `bedrock_usage` rows)?
