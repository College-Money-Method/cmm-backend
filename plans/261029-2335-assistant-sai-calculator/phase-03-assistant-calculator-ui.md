# Phase 3: Assistant calculator UI

> **Revised 2026-10-29:** the result is sent as structured `calculator_result` (all of the formula's flat fields) alongside a short chat message, saved per conversation in localStorage, and never stored server-side. The marker is suppressed once results exist unless the family asks to redo them. See plan.md Decisions; where this file disagrees, plan.md wins.

Priority: high · Status: todo · Repo: cmm-frontend · Effort: 1d

## Context
- `app/routes/assistant.tsx`: loader (token, conversations).
- `app/components/assistant/`:
  - `assistant-app.tsx` (146 lines)
  - `assistant-message-list.tsx`: `STARTER_QUESTIONS`, `AssistantAnswer`
  - `assistant-composer.tsx`
- `app/hooks/use-assistant-chat.ts`: `send(text, profile)`. It does not change; the result is plain text.
- `app/lib/assistant/render-assistant-markdown.ts`: escapes and allowlists the model output.
- `app/components/content/calculator-iframe.tsx`: `CalculatorIframe({slug, title, minHeight})`.
- `app/components/ui/sheet.tsx` exists.

## Requirements
1. **Slug lookup.** The `/assistant` loader calls `calculators.listPublicSlugs()` and picks `type === "fafsa_sai"`, preferring the latest `updated_at`. It returns `saiCalculator: {slug, title} | null`. On failure or no match it returns null, and the button and chip are hidden; the chat never breaks.
2. **Markers** (new `app/lib/assistant/assistant-calculator-marker.ts`):
   - `extractCalculatorMarkers(text) → {text, calculators: ("fafsa_sai")[]}` removes every `[[calculator:…]]` and keeps only known ids.
   - While streaming, it also hides a trailing partial `[[…` so the marker never flashes.
   - It applies to live and reloaded history.
3. **Button.** `AssistantAnswer` renders "Estimate my SAI" under the answer (not while streaming), and only when `saiCalculator` exists.
4. **Starter chip.** The empty state gets a fourth chip, "Estimate our Student Aid Index (SAI)". It opens the sheet directly, without a chat turn.
5. **Sheet** (new `assistant-calculator-sheet.tsx`):
   - It is a right-side `Sheet`, full width under `sm`.
   - It holds `CalculatorIframe` with a new `chat` prop that adds `&chat=1`.
   - The listener accepts a message only if:
     - `event.origin === location.origin`;
     - `event.source === iframe.contentWindow`;
     - the type and slug match;
     - the numbers are finite and `pell_status` is in a known set.
   - The footer shows the latest result as "SAI $X · Pell …", a "Use this result" button (disabled until a result arrives), and a note: "Sends only your SAI and Pell estimate to the assistant — not the numbers you entered."
6. **Send.** `formatCalculatorResultMessage(result)` produces "I ran the SAI calculator (2027–28): SAI $8,450 · Pell: partial, about $2,100/yr. What does this mean for us?". It is sent through the existing `send(text, profile)`, after which the sheet closes. A negative SAI (minimum −1500) is formatted as "−$1,500".

## Files
- Create:
  - `app/lib/assistant/assistant-calculator-marker.ts` (marker extraction, result validation, message format)
  - `app/components/assistant/assistant-calculator-sheet.tsx`
- Modify:
  - `app/routes/assistant.tsx` (loader)
  - `app/components/assistant/assistant-app.tsx` (sheet state, send)
  - `app/components/assistant/assistant-message-list.tsx` (marker strip, button, chip)
  - `app/components/content/calculator-iframe.tsx` (`chat?: boolean`)
- Tests: `assistant-calculator-marker.test.ts`:
  - strip known and unknown ids;
  - partial marker mid-stream;
  - validation rejects NaN, strings and missing fields;
  - message format, including a negative SAI.

## Steps
1. Write the marker and format helpers with tests (pure, first).
2. Add the `chat` prop to `CalculatorIframe`.
3. Build the sheet component and listener.
4. Wire up the loader, the app state, the message-list button and the chip.
5. Run `pnpm typecheck`, `pnpm lint` and the focused vitest.

## Todo
- [ ] helpers + tests
- [ ] iframe `chat` prop
- [ ] sheet + listener
- [ ] loader slug lookup
- [ ] button + chip wiring
- [ ] typecheck/lint/tests

## Success criteria
The full loop works locally: ask, then button, then run the calculator, then "Use this result", then an explained reply. The marker is never visible.

## Risks
- **Mobile height:** the iframe resizes itself, and the sheet must scroll its body rather than the page. Check at 375px wide.
- **The user edits the result before sending:** not supported. The message is generated, and the user can type a follow-up.
- **Sheet focus trap vs the iframe:** the base Sheet should allow focus into the iframe. Verify keyboard tabbing.

## Security
- Strict origin and source checks on the message.
- Values are re-validated in the parent, so a rogue frame cannot inject arbitrary text into the chat.
- The text is built from validated numbers only.
