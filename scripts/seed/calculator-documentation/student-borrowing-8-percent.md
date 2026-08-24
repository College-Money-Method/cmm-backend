# Student Borrowing: the 8% Rule

- **Slug:** `student-borrowing-8-percent` · **Type:** `student_borrowing_8_percent`
- **Embed:** `/embed/calculators/student-borrowing-8-percent` · **On-site:** `/calculators/student-borrowing-8-percent`
- **Source of truth:** the `calculators` row. The markup and config are
  authored in the frontend repo at `app/lib/calculators/student-borrowing-8-percent/`; this
  brief is authored in the backend repo at
  `scripts/seed/calculator-documentation/student-borrowing-8-percent.md`. Both are pushed into
  the row by `scripts/seed/seed_calculators.py`, and after seeding, CMS
  edits win until the next run.

Read this before changing anything here. The tax row in particular is a
deliberate rewrite of the source spreadsheet — see §5 before "fixing" it back.

---

## 1. How it gets its data

**One rule: the markup never hard-codes a dollar figure or a rate.** Every
yearly number is read from `window.__CALC_CONFIG__`, so a policy update is a
Data-tab edit, not a deploy.

```
Postgres calculators row (html + config JSONB)
        │  GET /api/v1/calculators/public/student-borrowing-8-percent   ← published only, no auth, no cookie
        ▼
buildCalculatorDocument()        app/lib/calculators/build-calculator-document.ts
  ├─ <head>: charset, viewport, robots=noindex, color-scheme=light
  ├─ iframeThemePreamble()       brand fonts + --cmm-* vars
  ├─ buildCalculatorDeps(config) <script>window.__CALC_CONFIG__={…}</script> + row.deps
  ├─ <style>  --calc-bg from ?bg=  (transparent by default)
  ├─ <body>   row.html VERBATIM, UNSANITIZED
  └─ resize script → postMessage {type:"cmm-calculator-resize", slug, height}
```

Four surfaces render that same document — public embed, on-site page, CMS block,
admin preview — so none can silently differ. No cookie or session is read on the
embed routes; `Cache-Control: public, no-cache` + ETag means every load
revalidates; `frame-ancestors` is sent only when the allowlist is non-empty.

## 2. The contract the markup must keep

| Global | Direction | Rule |
|---|---|---|
| `window.__CALC_CONFIG__` | injected | the `config` bag verbatim |
| `window.__CALC_RUN__(inputs)` | authored | **pure** — no `document`, no `window.location`, no `fetch`, no `localStorage`. Returns one flat result object |
| `window.__CALC_SELFTEST__` | authored | `[{name, inputs, expect, tolerance?}]`. Numbers compare within `tolerance`, everything else strict `===` |

The formula is the only script CI evaluates, in `node:vm` with nothing but
`{window:{__CALC_CONFIG__:config}}`. The UI script beside it is the only part
allowed to touch the document. The form calls `preventDefault`; nothing is POSTed
and no PII leaves the browser.

**Inputs:** `annual_salary`, `housing_percent`, `savings_percent`,
`extra_borrowing`.

**Result fields** — the borrowing ceiling (`monthly_salary`, `payment_share`,
`max_monthly_payment`, `max_borrowing`, `loan_term_months`); the budget
(`federal_tax_annual`, `payroll_tax_monthly`, `taxes_monthly`, `taxes_percent`,
`loan_monthly`, `loan_percent`, `housing_monthly`, `housing_percent`,
`savings_monthly`, `savings_percent`, `daily_monthly`, `daily_percent`,
`budget_total`, `budget_total_percent`, `over_budget`, `overage`); the growth
projection (`projections[]` `{years, value, contributed}`, `total_contributed`,
`baseline_final_value`); and the borrow-more comparison (`extra_borrowing`,
`new_principal`, `new_monthly_payment`, `additional_payment`,
`new_savings_monthly`, `savings_exhausted`, `new_projections[]`,
`new_final_value`, `projection_cost`).

## 3. Config — 14 keys

| Group | Keys |
|---|---|
| The rule | `payment_share_of_salary` .08, `loan_interest_rate` .0652, `loan_rate_note`, `loan_term_years` 10 |
| Budget defaults | `default_housing_percent` .30, `housing_ceiling_percent` .35, `default_savings_percent` .10 |
| Taxes | `standard_deduction` 15750, `payroll_tax_rate` .0765, `tax_brackets[7]` `{floor, rate}` |
| Savings growth | `investment_return_rate` .08, `contribution_years` 10, `projection_milestones[3]` `{years}` |
| Borrowing more | `default_extra_borrowing` 5000 |

Note `tax_brackets` is `{floor, rate}` — **no `base` column**, unlike the two SAI
bracket tables. The bands are summed for the author, so there is no denormalised
total to keep in step. Do not add a `base` here.

`projection_milestones`' last entry is the horizon the "what it costs you"
sentence quotes.

## 4. What it computes — four sections

1. **Ceiling.** salary ÷ 12 → × 8% → `−PV(r/12, 120, pmt)` for the maximum total
   borrowing.
2. **Budget.** A 5-row monthly budget: taxes and the loan payment locked, housing
   and savings editable, "everything else" the remainder.
3. **Growth.** Contribute the savings row for 10 years at 8%, then coast to each
   milestone.
4. **Borrowing more.** `−PMT` on a bigger balance, with the extra payment charged
   to the savings row — and the lost compounding quoted as `projection_cost`.

## 5. Deliberate divergences from the source spreadsheet

Three, all deliberate. **The tax rewrite is the one to understand before
touching anything:**

1. **The tax row is rewritten.** The sheet's `D26`:
   - adds FICA twice below its first threshold (a stray parenthesis — the trailing
     `+(D14*0.07625)` sits outside the inner `IF`, and the true branch already
     includes it),
   - applies 2022 brackets to **gross** salary with no standard deduction,
   - types the threshold two different ways (`41775` in the test, `41755` in the
     `≥` branch),
   - and uses 7.625% against a real 7.65%.

   Replaced with a proper marginal walk on income after the standard deduction,
   payroll tax counted once, all config-driven (2025 single filer). **At $40,000
   that is $477.63/mo (14.3%) instead of the sheet's $891.17 (26.7%)** — anyone
   comparing against the old worksheet will notice, and that is expected.

2. **"Everything else" is the remainder, not a rate,** so the budget always sums
   to the whole salary. A negative remainder turns the total row coral and names
   the overage. The sheet also shrank savings automatically as the loan share rose
   past 8%; savings now stays under the reader's control.

3. **One column, not three.** The sheet duplicates itself into columns I/J and
   O/P for "compare 3 majors", and both copies are stale: `J65` points at Daily
   expenses instead of Savings, `J68`/`P68` compound monthly where `D68` compounds
   annually, and their notes still say 2022-23 / 3.73% against a rate cell reading
   5.5%. **Nothing was ported from them.**

Every amortisation and growth figure still reproduces the sheet to the cent.

## 6. Tests

11 cases in `window.__CALC_SELFTEST__`, run by the admin Markup tab's **Checks**
button and by CI (`app/lib/calculators/__tests__/authored-selftests.test.ts`,
which auto-discovers any directory holding both `calculator.html` and
`config.json`). This is the one calculator where `tolerance` matters — currency
cases run at 0.01–0.02 and compounding cases at 1–2.

| Case | Locks |
|---|---|
| No salary, no ceiling and no budget | zero in, zero out, not "over budget" |
| A negative salary is a typo, not a negative budget | the salary floor |
| The 8% ceiling on a $40,000 salary buys $23,464 of loans | the `−PV` amortisation, reproducing the sheet |
| Federal tax after the standard deduction, payroll on all of it | the rewritten tax row: 2,672/yr, 255/mo, 477.63 total (§5.1) |
| The budget spends the whole salary and nothing more | the remainder row: 3,333.33 sums exactly |
| Housing and savings a family cannot afford are over budget | the coral overage path, 244.29 |
| Ten years of saving becomes the worksheet's 40-year figure | contribute-then-coast growth, 583,093 |
| Borrowing $5,000 more costs $56.82 a month, from savings | `−PMT` on 28,464 → 323.49, savings 276.51 |
| And that $5,000 costs six figures of retirement | `projection_cost` 99,402 |
| A payment bigger than the savings row empties it | `savings_exhausted`, no negative savings |
| Zero savings leaves nothing to compound | the degenerate growth case |

Locked figures from the sheet: $23,464 borrowed at a $40,000 salary, $323.49 on
+$5,000, and $57,946 / $183,815 / $583,093 at $333.33/mo.

## 7. Updating this calculator

1. `loan_interest_rate` (with its `loan_rate_note`), `standard_deduction`,
   `payroll_tax_rate` and `tax_brackets` are the yearly edits — **all Data-tab**,
   no markup change, no deploy.
2. Changing `loan_interest_rate`, `investment_return_rate`, `standard_deduction`
   or the brackets **will** move the locked self-test figures. That is correct;
   recompute the expectations rather than loosening `tolerance`.
3. `tax_brackets` has no `base` column by design (§3).
4. `projection_milestones` is what the growth table and the closing sentence both
   read — changing the last entry changes the copy.
5. Expect 11/11 green before saving a published row; the publish gate enforces it.
6. Adding or renaming a config key means editing the
   `student_borrowing_8_percent` descriptor list in `config-schema.ts` too —
   it was `[]` once, and the Data tab fell back to raw JSON.

## 8. Creating a new calculator from this pattern

1. Create the row at `/admin/calculators/new` — always a draft. `type` is fixed
   after creation because the markup reads config fields by name.
2. Add the new type to `CALCULATOR_TYPES` in **both** `app/types/calculators.ts`
   and `src/calculators/models.py`, plus a label in `CALCULATOR_TYPE_LABELS`.
3. Add a descriptor list for the type in `config-schema.ts` — never leave it
   empty.
4. Author `app/lib/calculators/<slug>/calculator.html` and `config.json`. The
   markup is a **fragment** — several `<script>` elements, no document wrapper.
   Name the formula script `id="<prefix>-formula"`; CI matches
   `<script id="[\w-]*formula">`.
5. Declare `__CALC_SELFTEST__` cases from day one — CI asserts every authored
   calculator declares at least one. For money, set an explicit `tolerance`; for
   compounding, 1–2 is enough and anything looser stops catching real drift.
6. Call `preventDefault` on every form.
7. Write the brief at `scripts/seed/calculator-documentation/<slug>.md` in
   the backend repo — it is what the Documentation tab shows.
8. Register the slug in `CALCULATORS` in `scripts/seed/seed_calculators.py`,
   then seed with an explicit `--source` and `--env-file`.

## Open questions

1. **Accept the corrected tax model** over the sheet's formula? It roughly halves
   the tax row at low salaries versus the old worksheet.
2. Brackets are single-filer, federal only — no state tax, no married rates. Add a
   filing-status question, or is single-filer right for a new graduate?
3. The sheet's three-way "compare 3 majors" layout was dropped. Second pass if
   wanted — a layout change, not a config change.
