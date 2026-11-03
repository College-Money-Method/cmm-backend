# Verify + review — assistant SAI calculator (2026-10-30)

Local stack: backend :8001, Supabase Postgres :54322, Vite :5173, debug Chrome (one tab). Nothing committed.

## Static
- Frontend: `pnpm typecheck` clean; eslint + prettier clean on changed files; `pnpm test` 53 files / 497 tests pass.
- Backend: `uv run pytest tests/assistant tests/calculators -q` 72 pass.

## E2E (real LLM, real seeded formula)
- Starter chip opens sheet with `/embed/calculators/fafsa-sai-2027-28?chat=1`; wrapped `__CALC_RUN__` passes 11/11 self-tests.
- Untouched defaults → results: nothing posted, "Use this result" disabled.
- Typed income → summary + button enabled; "Use this result" → reply quotes SAI/Pell from the result, no recompute, no button; localStorage holds the result (36 fields) for that conversation.
- Reload conversation → follow-up still uses stored result; no button.
- "Redo the numbers" → button returns (one), raw marker never shown. Fresh chat SAI question → button, marker hidden.
- Edit an earlier step after a result → sheet result withdrawn (button disabled); back on results → recomputed result posted.
- Partner embed (no `?chat=1`): no bridge, resize script intact. Chat variant has bridge, ETag `-chat`.
- Mobile 390px: sheet full width, footer + copy fit.

## Review findings (code-reviewer) → action
- H1 stale result in sheet: fixed. Bridge clears pending post on each run, re-checks results step when firing, posts `result: null` when a posted result goes stale (edit off results / invalid output); sheet clears on it. Tests added.
- M1 privacy copy overstated: reworded ("headline numbers appear in the chat; full results stay on this device…").
- M2 results kept after sign-out: Sign out now clears all `cmm_assistant_calc:*` keys. Full field dump kept per user decision ("include all the results").
- M3 key regex `$` allowed trailing newline / control chars in text: backend `fullmatch`, control chars rejected; mirrored in frontend sanitizer + bridge. Tests added.
- L1 one-frame marker flash: partial-tail regex accepts one `]`. Test added.
- L2 huge ints: values bounded to |x| ≤ 1e12 both sides (comparison safe for 5000-digit ints; raw-body test → 4xx).
- L3 storage blocked → result lost on reload: accepted; comment corrected.
- L4 marker kept in history: not changed — it is how the model knows it already offered; UI shows at most one button.

## Found during verify
- Embed ETag ignored wrapper code → after a deploy, browsers got 304 and kept the old bridge/resize script until an admin edited the calculator. ETag now leads with a 12-char fingerprint of the wrapper (built once around a stub). Partner embeds revalidate once after each wrapper change.

## Unresolved
- None blocking. Optional later: College Aid chaining (deferred by decision).
