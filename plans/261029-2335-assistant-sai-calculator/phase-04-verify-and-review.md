# Phase 4: Verify end to end + review

Priority: high · Status: todo · Repos: both · Effort: 0.5d

## Checks
1. **Self-test.** In the debug Chrome (one tab, port 8222), open local `/embed/calculators/fafsa-sai-2027-28?chat=1`. `window.__CALC_SELFTEST__` must still pass 16/16 with the wrapper installed.
2. **No result on defaults.** Load the page untouched and wait: no `cmm-calculator-result` message.
3. **Allowlist.** Change inputs. Capture the posted message in the parent and confirm its keys are exactly `{sai, pell_status, pell_award, award_year}`.
4. **Network.** Send the result from `/assistant`. The `/assistant/chat` request body contains only the generated sentence, with no input values.
5. **Answer quality.** The reply quotes the same SAI and does not recalculate.
6. **Marker discipline.** Ask 5 unrelated questions (divorced parents, CSS Profile, merit aid, loans, deadlines) and expect no marker. Ask 3 SAI-related questions and expect the marker each time.
7. **Reload.** History shows the button, not the raw marker.
8. **Partner regression.** Fetch the embed without `?chat=1`; it is byte-identical to `main`. A foreign-origin test page with `?chat=1` receives nothing.
9. **Mobile.** The sheet at 375px is usable and the calculator scrolls.
10. **Review.** Run the code-reviewer agent over both diffs. Fix any confirmed findings.

## Done when
All checks pass. Results are noted in `reports/verify-sai-calculator.md`. Ship only after the accuracy sign-off from the unresolved question in plan.md.
