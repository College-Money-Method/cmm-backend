# Phase 1: Backend prompt rules + calculator type

> **Revised 2026-10-29:** the result is sent as structured `calculator_result` (all of the formula's flat fields) alongside a short chat message, saved per conversation in localStorage, and never stored server-side. The marker is suppressed once results exist unless the family asks to redo them. See plan.md Decisions; where this file disagrees, plan.md wins.

Priority: high · Status: todo · Repo: cmm-backend · Effort: 0.5d

## Context
- `src/assistant/answer.py`: `_SYSTEM_TEMPLATE` already carries the follow-up-question rule. That rule is still uncommitted; commit it first or together with this phase.
- `src/calculators/router.py:42` `list_published_calculator_slugs`, `src/calculators/schemas.py:98` `CalculatorSlugOut`.
- Prod calculator: slug `fafsa-sai-2027-28`, type `fafsa_sai`. The slug changes every year.

## Requirements
1. **Marker rule.** When the family asks about their own SAI, Pell eligibility, or how much they would be expected to pay, the model:
   - answers briefly;
   - says CMM has a calculator that estimates the SAI privately in the browser;
   - ends with the exact line `[[calculator:fafsa_sai]]`;
   - adds the marker at most once per answer;
   - never adds it for counselors asking general questions unless they ask for a family estimate.
2. **Result rule.** When a user message says "I ran the SAI calculator…", the model:
   - treats the numbers as computed by CMM's calculator;
   - explains what they mean (SAI range, Pell, the gap between cost of attendance and SAI, next steps);
   - never recalculates, adjusts, or questions the arithmetic;
   - calls it an estimate;
   - does not ask for the inputs behind it.
3. **Calculator type.** `CalculatorSlugOut` gains `type: str`, and the public list selects `Calculator.type`. The change is additive.

## Files
- Modify:
  - `src/assistant/answer.py` (2 prompt lines)
  - `src/calculators/schemas.py`
  - `src/calculators/router.py`
- Tests:
  - `tests/assistant/test_search_and_prompt.py`: the prompt contains the marker and result rules.
  - The calculator router test for the public list: the item has `type`.

## Steps
1. Add the two rules to `_SYSTEM_TEMPLATE`, just above "Be warm…". Keep them short, because the prompt is cached per request.
2. Add `type` to the schema and the select.
3. Run `uv run pytest tests/assistant tests/calculators -q` and `uvx ruff check --select E9,F`.

## Todo
- [ ] prompt: marker rule
- [ ] prompt: calculator-result rule
- [ ] `CalculatorSlugOut.type` + select
- [ ] tests green

## Success criteria
A local chat question "what will our SAI be?" ends with the marker line. A pasted result message gets an explanation without recomputing.

## Risks
- **The model over-uses the marker.** Mitigation: the rule says "only when they ask about their own numbers". Phase 4 checks 5 unrelated questions.
- **The model invents a different marker spelling.** Mitigation: the frontend strips any `[[calculator:*]]` it does not know (phase 3).

## Security
No new input surface. The result arrives as ordinary chat text, already length-capped at 2000 characters.
