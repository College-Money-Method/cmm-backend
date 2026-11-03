# Phase 2: Embed result bridge (`?chat=1`)

> **Revised 2026-10-29:** the result is sent as structured `calculator_result` (all of the formula's flat fields) alongside a short chat message, saved per conversation in localStorage, and never stored server-side. The marker is suppressed once results exist unless the family asks to redo them. See plan.md Decisions; where this file disagrees, plan.md wins.

Priority: high · Status: todo · Repo: cmm-frontend · Effort: 0.5d

## Context
- `app/lib/calculators/build-calculator-document.ts` is 193 lines: `buildCalculatorDocument`, `resizeScript`. The body is `${calc.html}${resizeScript(slug)}`.
- `app/routes/embed/calculator-document.ts`: query params, ETag, frame-ancestors. No cookie or session read; keep it that way.
- In the prod SAI markup, the `sai-formula` script defines `window.__CALC_RUN__(inputs) → out`. `sai-ui` `render()` calls `window.__CALC_RUN__` **at call time**, so wrapping the global after the body runs catches every recalculation.
- `out` also echoes inputs (`parent_cash`, `parent_investments`, `parent_child_support`, …), so an allowlist is required.

## Requirements
- `buildCalculatorDocument(calc, {…, chat: true})` appends `chatResultScript(slug)` after `calc.html`. Without `chat`, the output is byte-identical to today; a test enforces this.
- The script:
  1. sets `touched = true` on the first trusted `input` or `change` event in the document, so default values never post a result;
  2. wraps `window.__CALC_RUN__` if it is a function, calls the original, and returns its value unchanged; exceptions propagate unchanged;
  3. debounces 400 ms, then, if touched, posts `{type:'cmm-calculator-result', slug, result:{sai, pell_status, pell_award, award_year}}`:
     - `award_year` comes from `window.__CALC_CONFIG__.award_year`;
     - numbers are posted only if finite, and `pell_status` only if it is a string;
  4. posts with `window.parent.postMessage(msg, location.origin)`. A foreign parent is dropped by the browser;
  5. does nothing if `__CALC_RUN__` is missing; the calculator still works.
- The route reads `?chat=1` and adds `chat` to the ETag key.
- Export `CALCULATOR_RESULT_MESSAGE` and document it as internal. It is not a partner contract and can change.

## Files
- Create: `app/lib/calculators/calculator-chat-result-script.ts`. Keep it separate to hold `build-calculator-document.ts` under about 200 lines.
- Modify:
  - `app/lib/calculators/build-calculator-document.ts`
  - `app/routes/embed/calculator-document.ts`
  - `app/types/calculators.ts`: a comment on `fafsa_sai` saying the chat bridge depends on `window.__CALC_RUN__` being looked up at call time and returning `sai`, `pell_status` and `pell_award`.
- Tests: extend the existing `build-calculator-document` tests (look for `*.test.ts` next to it):
  - no `chat` → unchanged output;
  - `chat` → script present after the body and before `</body>`;
  - allowlist keys only.

## Steps
1. Write the script builder (string template, like `resizeScript`; `JSON.stringify` for the slug).
2. Wire the option and the route param, including the ETag.
3. Add unit tests. Also add a small jsdom test that runs the script against a stub `__CALC_RUN__` returning input echoes: only the allowlisted keys are posted, and only after an input event.

## Todo
- [ ] chat result script + allowlist
- [ ] `chat` option + `?chat=1` + ETag
- [ ] calculator type comment
- [ ] tests

## Success criteria
`/embed/calculators/fafsa-sai-2027-28?chat=1` locally: typing in a field posts one result message per pause. The partner URL without `?chat=1` is unchanged.

## Risks
- **An admin edits the markup to cache `__CALC_RUN__` in a local variable.** The bridge then goes silent and the button stays disabled, so the failure is safe but invisible. Mitigation: the types comment, plus a phase 4 check to re-run after any SAI markup edit.
- **The embed route is publicly cached** (`public, no-cache` + ETag). Mitigation: put `chat` in the ETag so the two variants never share a 304.

## Security
- Same-origin document, unsandboxed; this is unchanged.
- The targetOrigin is pinned to our origin, so `?chat=1` gives a partner nothing.
- No input values ever leave the frame.
