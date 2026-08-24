# Student borrowing: the 8% rule

- **Slug:** `student-borrowing-8-percent` · **Type:** `student_borrowing_8_percent`
- **Embed:** `/embed/calculators/student-borrowing-8-percent` · **On-site:** `/calculators/student-borrowing-8-percent`
- **Where it lives:** the `calculators` database row — the markup in the Markup
  tab, every rate and default in the Data tab. Editing the row changes the live
  calculator.

**If you are an AI agent editing this calculator:** you have been given the
markup. Read §2 and §6 before you change it. Styling, copy and layout are yours
to change freely; the attributes in §2 are the wiring, and removing one stops the
calculator dead. Nothing in §4 is a bug to fix — the tax row in particular looks
wrong against older worksheets and is not.

---

## 1. What it does

Starts from the salary a major or career actually pays and works backwards to the
most a student should borrow for the whole degree: 8% of gross monthly pay is the
most that should go to loan payments, and a 10-year loan at a given rate turns
that payment into a total. Then it shows what living on that salary looks like,
what the savings line becomes if invested, and what borrowing beyond the rule
costs in retirement.

Four sections, in DOM order:

1. **The ceiling** — salary in, maximum borrowing out.
2. **A sample monthly budget** — five rows; taxes and the loan payment are locked,
   housing and savings are the reader's to set, "everything else" is what's left.
3. **What that savings becomes** — the savings row compounded, with milestones.
4. **What if you borrow more?** — the extra payment charged to the savings row,
   with the lost compounding priced.

## 2. How the file is wired

The markup is a **fragment** — a `<style>` block, the HTML, then two `<script>`
elements. It is injected into a document that is built around it, so it must
never contain `<html>`, `<head>`, `<body>` or `<!doctype>`.

| Part | Role |
|---|---|
| `<style>` | all styling, scoped by `.cmm-calc` and `.calc-*` classes |
| `<div class="cmm-calc">` | the root. The UI script gives up silently without it |
| `<script id="borrow-formula">` | the math. Pure — no DOM, no network, no storage |
| `<script id="borrow-ui">` | reads the four fields, calls the formula, paints everything |

Markup and logic are joined by attributes, not by structure — so you can move,
rewrap and restyle anything as long as the attributes travel with it:

| Attribute | Meaning |
|---|---|
| `data-calc-form` | the form the UI script listens to |
| `name="annual_salary" \| "housing_percent" \| "savings_percent" \| "extra_borrowing"` | the four inputs |
| `data-out="<field>"` | an element the UI script writes a result into — ~40 of them |
| `data-projection-rows` | the `<tbody>` the growth rows are generated into |
| `data-total` | the budget total row; the script sets `data-over` on it |
| `data-over` | `true` when the budget overspends — styling hook, script-set |
| `data-locked` | marks the rows the reader cannot edit; its `::after` prints "· set for you" |
| `data-cost-note` | the closing sentence, shown or hidden by the script |

This calculator has by far the most `data-out` bindings — roughly forty, because
almost every number and rate label in the prose is painted from the result rather
than typed. Labels like `rate_label`, `term_label`, `share_label`,
`return_label`, `payroll_label` and `deduction_label` exist so that changing a
config value updates the sentences too. **Keep them.** Typing "8%" into the copy
instead is how the page starts lying after a rate change.

Two things about `data-out` that bite:

- The script looks it up with `querySelector`, so **only the first match is
  painted.** That is why a few figures appear twice under paired names —
  `savings_monthly` / `savings_monthly_echo`, `return_label` / `return_label_2`,
  `share_label` / `share_label_2`. To show a figure in a third place, add another
  name in the UI script rather than duplicating an existing one.
- The names in §3 are painted unconditionally. **Delete the element and the paint
  throws**, which kills the whole render — every figure freezes and typing stops
  doing anything. Hide it with CSS if you don't want it seen.

Config arrives as `window.__CALC_CONFIG__` before these scripts run. The form
calls `preventDefault`, so a submit button is safe to add; nothing is ever sent
anywhere, and no figure a family types may reach a URL, a log or a request.

## 3. Inputs, results, config

**Inputs:** `annual_salary`, `housing_percent`, `savings_percent`,
`extra_borrowing`.

**Results**, by section:

| Section | Fields |
|---|---|
| Ceiling | `monthly_salary`, `payment_share`, `max_monthly_payment`, `max_borrowing`, `loan_term_months` |
| Budget | `federal_tax_annual`, `payroll_tax_monthly`, `taxes_monthly`, `taxes_percent`, `loan_monthly`, `loan_percent`, `housing_monthly`, `housing_percent`, `savings_monthly`, `savings_percent`, `daily_monthly`, `daily_percent`, `budget_total`, `budget_total_percent`, `over_budget`, `overage` |
| Growth | `projections[]` `{years, value, contributed}`, `total_contributed`, `baseline_final_value` |
| Borrow more | `extra_borrowing`, `new_principal`, `new_monthly_payment`, `additional_payment`, `new_savings_monthly`, `savings_exhausted`, `new_projections[]`, `new_final_value`, `projection_cost` |

**Config — 14 keys**, all Data-tab edits, no deploy:

| Group | Keys |
|---|---|
| The rule | `payment_share_of_salary` .08, `loan_interest_rate` .0652, `loan_rate_note`, `loan_term_years` 10 |
| Budget defaults | `default_housing_percent` .30, `housing_ceiling_percent` .35, `default_savings_percent` .10 |
| Taxes | `standard_deduction` 15750, `payroll_tax_rate` .0765, `tax_brackets[7]` `{floor, rate}` |
| Savings growth | `investment_return_rate` .08, `contribution_years` 10, `projection_milestones[3]` `{years}` |
| Borrowing more | `default_extra_borrowing` 5000 |

`tax_brackets` is `{floor, rate}` — **no `base` column**, unlike the SAI bracket
tables. The bands are summed at run time, so there is no denormalised total to
keep in step. Do not add one.

## 4. Rules the numbers follow — do not "fix" these

- **The tax row is a marginal walk on income after the standard deduction, with
  payroll tax counted once.** Older copies of this worksheet computed it very
  differently — double-counting FICA below the first threshold, applying stale
  brackets to gross pay with no deduction, and using 7.625% for a 7.65% tax. At a
  $40,000 salary the correct model gives **$477.63/mo (14.3%)** where the old
  sheet gave $891.17 (26.7%). Anyone comparing against an old spreadsheet will
  see a gap; that gap is the fix, not a bug.
- **"Everything else" is the remainder, not a percentage,** so the budget always
  sums to the whole salary. When housing and savings overspend it, the total row
  turns coral and names the overage rather than silently shrinking a row.
- **Savings stays under the reader's control.** It is never auto-reduced to make
  room for a bigger loan payment.
- **A negative salary is treated as a typo and floored**, not as a negative budget.
- **Borrowing more is charged to savings**, and when the extra payment exceeds the
  savings row, savings empties and stops — `savings_exhausted` — instead of going
  negative.
- **Brackets are federal, single-filer.** No state tax, no married rates. That is a
  deliberate simplification for a new graduate, not an omission to patch inline.

## 5. Self-tests

11 cases live in `window.__CALC_SELFTEST__` at the bottom of the formula script.
The **Checks** button on the Markup tab runs them; a published row is expected to
be 11/11 green. This is the one calculator where `tolerance` matters — currency
cases run at 0.01–0.02, compounding cases at 1–2.

| Case | What it locks |
|---|---|
| No salary, no ceiling and no budget | zero in, zero out, not "over budget" |
| A negative salary is a typo, not a negative budget | the salary floor |
| The 8% ceiling on a $40,000 salary buys $23,464 of loans | the `−PV` amortisation |
| Federal tax after the standard deduction, payroll on all of it | the tax model: 2,672/yr, 255/mo, 477.63 total |
| The budget spends the whole salary and nothing more | the remainder row sums to 3,333.33 exactly |
| Housing and savings a family cannot afford are over budget | the coral overage path, 244.29 |
| Ten years of saving becomes the 40-year figure | contribute-then-coast growth, 583,093 |
| Borrowing $5,000 more costs $56.82 a month, from savings | `−PMT` on 28,464 → 323.49, savings 276.51 |
| And that $5,000 costs six figures of retirement | `projection_cost` 99,402 |
| A payment bigger than the savings row empties it | `savings_exhausted`, no negative savings |
| Zero savings leaves nothing to compound | the degenerate growth case |

Anchor figures: $23,464 borrowed at a $40,000 salary, $323.49 on +$5,000, and
$57,946 / $183,815 / $583,093 at $333.33/mo.

## 6. Editing checklist

Restyling or reordering:

1. Keep every attribute in §2 on some element, and keep `.cmm-calc` as an
   ancestor of all of them.
2. Keep the fragment a fragment — no document wrapper, no external stylesheet or
   font link, no `fetch`, no `localStorage`.
3. Keep both scripts, with their `id`s. `borrow-formula` is run on its own by the
   test harness; renaming it hides the calculator from the checks.
4. Do not replace a `data-out` label with typed-in text, however static it looks
   (see §2).
5. `data-locked`, `data-over` and `data-total` are styling hooks the script sets.
   Restyle them; do not set them by hand.
6. Run the Checks button. 11/11 before saving a published row.

Changing the numbers: `loan_interest_rate` (with its `loan_rate_note`),
`standard_deduction`, `payroll_tax_rate` and `tax_brackets` are the yearly edits,
all Data-tab. Editing any of them, or `investment_return_rate`, **will** move the
locked self-test figures — that is correct. Recompute the expectations rather
than loosening `tolerance`. `projection_milestones` drives both the growth table
and the closing sentence, so changing the last entry changes the copy.
