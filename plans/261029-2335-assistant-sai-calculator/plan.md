# Assistant: run the FAFSA SAI calculator from chat

Status: done (built, E2E verified locally, reviewed) · not committed · Owner: Vu · Created 2026-10-29 · Spans cmm-backend + cmm-frontend
Builds on: `plans/261029-2008-assistant-chat-prototype/plan.md`

## Outcome
A family asking about SAI, Pell, or "what will we pay" can open the existing embedded SAI calculator inside the chat, run it, and send the result back with one click. The assistant then explains the numbers. It never computes an SAI itself.

## Decisions (agreed 2026-10-29)
- **Reuse the embed, do not port the formula.** The SAI and its yearly tables stay in one place: `window.__CALC_RUN__` plus `__CALC_CONFIG__`, which admins edit.
- **The result comes back as a short chat message plus a structured `calculator_result`** (revised 2026-10-29 by the user):
  - The visible message is a summary, for example "I ran the SAI calculator (2027-28): SAI $8,450 · Pell Grant: partial, about $2,100/yr · parent contribution $X · student contribution $Y. What does this mean for us?"
  - Every flat primitive field the formula returns (about 50) is kept in the browser's localStorage under `cmm_assistant_calc:{conversation_id}` and sent with every later question in that conversation, like the profile.
  - The API validates it and puts it in the system prompt ("Calculator results:"). It is never stored server-side, and there is no migration.
- **Two ways to open it:**
  - The model ends a relevant answer with the marker `[[calculator:fafsa_sai]]`, and the UI turns it into an "Estimate my SAI" button on the latest answer only.
  - Once results exist (sent now or earlier in the chat), the model adds the marker only when the family asks to update or redo their numbers.
  - A starter chip on the empty chat does the same.
- **Bridge, only for the chat:**
  - `/embed/calculators/:slug?chat=1` adds a script that wraps `__CALC_RUN__`.
  - It posts the formula's flat result fields only to the app's own origin, and never uses `'*'`.
  - It posts only after a trusted input event and only when the wizard is on its results step.
  - Partner embeds are byte-identical to today.
- **Results leave the browser only on a click.** The formula output echoes some inputs (for example parent_cash), and the sheet says what is sent.
- **Calculator lookup is by type, never a hard-coded slug.** `CalculatorSlugOut` gains `type` (an additive change). The `/assistant` loader picks the published `fafsa_sai` calculator.

## Non-goals
- Feeding the SAI into College Aid per-college estimates. That is the next step: `src/colleges/public_router.py`, which takes sai, home_state and merit.
- Other calculator types.
- Storing results server-side or reporting on them.
- Bedrock tool use.
- Changing the calculator's math or markup.

## Contract changes
| Where | Change |
|---|---|
| `GET /api/v1/calculators/public` | Each item gains `type` (additive; the sitemap and embed picker ignore it). |
| `/embed/calculators/:slug?chat=1` | Adds a result bridge. Messages go to the parent with `postMessage(msg, location.origin)`. The ETag includes the flag. |
| postMessage `cmm-calculator-result` | Shape is `{type, slug, result}`, where `result` is flat primitives with a numeric `sai`. It is internal, not a partner contract. |
| `POST /api/v1/assistant/chat` | Optional `calculator_result {type:"fafsa_sai", values}`. Limits: at most 80 keys, keys match `^[a-z][a-z0-9_]{0,47}$`, strings at most 40 characters, finite numbers, numeric `sai`. Used in the prompt and never stored. |
| Chat marker | `[[calculator:fafsa_sai]]` on its own line at the end of an answer. Only allowlisted ids are rendered; anything else is removed. |

## Phases
| # | Phase | Repo | Effort | Status |
|---|---|---|---|---|
| 1 | [Backend: prompt rules + calculator type](phase-01-backend-prompt-and-calculator-type.md) | backend | 0.5d | done |
| 2 | [Embed result bridge](phase-02-embed-result-bridge.md) | frontend | 0.5d | done |
| 3 | [Assistant calculator UI](phase-03-assistant-calculator-ui.md) | frontend | 1d | done |
| 4 | [Verify end to end + review](phase-04-verify-and-review.md) | both | 0.5d | done — [verify report](reports/verify-261030-assistant-sai-calculator.md) |

Total is about 2.5 days. Phase 1 can run in parallel with phase 2. Phase 3 needs both.

## Acceptance
- "What's our SAI likely to be?" gets an answer ending in an "Estimate my SAI" button. The raw marker never shows, including mid-stream and in reloaded history.
- The button opens the calculator in a side sheet (full screen on phones).
  - "Use this result" is disabled until the family has changed an input and a result arrived.
  - Clicking it sends the chat message plus `calculator_result`, and the reply explains the SAI without recomputing or contradicting it.
- The result is saved per conversation in localStorage, sent with later questions, and removed when the conversation is deleted. The server never stores it.
- After a result exists, no "Estimate my SAI" button appears unless the family asks to update or redo their numbers.
- A partner embed without `?chat=1` is unchanged. With `?chat=1` inside a foreign parent, the parent receives nothing.
- `__CALC_SELFTEST__` passes every case with the bridge loaded. Backend and frontend tests, typecheck and lint pass.

## Resolved questions (user, 2026-10-29)
- Accuracy sign-off: the calculator is published and accurate, so no extra gate.
- What to send: all of the calculator's results, kept locally per conversation.
- Button frequency: whenever relevant, but not after calculating unless the family asks to update or redo.
