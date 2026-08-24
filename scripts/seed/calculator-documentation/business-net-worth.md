# Net worth of a business

- **Slug:** `business-net-worth` · **Type:** `business_net_worth`
- **Embed:** `/embed/calculators/business-net-worth` · **On-site:** `/calculators/business-net-worth`
- **Where it lives:** the `calculators` database row — the markup in the Markup
  tab, the numbers in the Data tab. Editing the row changes the live calculator.

**If you are an AI agent editing this calculator:** you have been given the
markup. Read §2 and §6 before you change it. Styling, copy and layout are yours
to change freely; the attributes in §2 are the wiring, and removing one stops the
calculator dead. Nothing in §4 is a bug to fix.

---

## 1. What it does

One input: the net worth of a family business or farm. The FAFSA does not count
that at full value — it discounts it on a federal schedule that is steepest on
the first dollars, exactly like tax brackets. The calculator walks the brackets,
shows the band-by-band table, and reports the amount actually counted as a parent
asset, plus a rough estimate of what that adds to the Student Aid Index.

The visible surface, in DOM order: heading, one-field form, a table of the
bracket schedule with a total row, a two-figure result block, and a collapsible
explanation.

## 2. How the file is wired

The markup is a **fragment** — a `<style>` block, the HTML, then two `<script>`
elements. It is injected into a document that is built around it, so it must
never contain `<html>`, `<head>`, `<body>` or `<!doctype>`.

| Part | Role |
|---|---|
| `<style>` | all styling, scoped by `.cmm-calc` and `.calc-*` classes |
| `<div class="cmm-calc">` | the root. The UI script gives up silently without it |
| `<script id="bnw-formula">` | the math. Pure — no DOM, no network, no storage |
| `<script id="bnw-ui">` | reads the field, calls the formula, paints the result |

Markup and logic are joined by attributes, not by structure — so you can move,
rewrap and restyle anything as long as the attributes travel with it:

| Attribute | Meaning |
|---|---|
| `data-calc-form` | the form the UI script listens to |
| `name="net_worth"` on the input | the key passed into the formula |
| `data-out="<field>"` | an element the UI script writes a result into |
| `data-rows` | the `<tbody>` the bracket rows are generated into |
| `data-applies` | set by the script on each generated row; style it, don't set it |

Two things about `data-out` that bite:

- The script looks it up with `querySelector`, so **only the first match is
  painted.** That is why the same number appears twice under two names —
  `adjusted_net_worth` in the table's total row and `adjusted_figure` in the
  result block. To show a figure in a third place, add another name in the UI
  script rather than duplicating an existing one.
- Every name in §3 is painted unconditionally. **Delete the element and the paint
  throws**, which kills the whole render — the calculator freezes showing `$0`
  and stops responding to typing. Hide it with CSS if you don't want it seen.

Config arrives as `window.__CALC_CONFIG__` before these scripts run. The form
calls `preventDefault`, so a submit button is safe to add; nothing is ever sent
anywhere, and no figure a family types may reach a URL, a log or a request.

## 3. Inputs, results, config

**Input:** `net_worth` — one number, taken as a string from the field.

**Results** returned by `window.__CALC_RUN__`:

| Field | What it is |
|---|---|
| `net_worth` | the input, floored at 0 |
| `brackets[]` | `{floor, ceiling, rate, amount}` per band — what the table draws |
| `applicable_bracket` | index of the band the net worth lands in, `-1` for zero |
| `adjusted_net_worth` | the amount counted as a parent asset |
| `adjusted_from_table` | the same figure by the published formula (see §4) |
| `sai_contribution` | the planning estimate |
| `sai_contribution_rate` | the rate behind that estimate, for the label |

Painted into the page: `award_year`, `top_threshold`, `adjusted_net_worth`,
`adjusted_figure`, `sai_contribution`, `rate_label`.

**Config — 3 keys**, all Data-tab edits, no deploy:

| Key | Value | Notes |
|---|---|---|
| `award_year` | `"2025-26"` | label only, nothing computes from it |
| `brackets[4]` | `{floor, base, rate}` — 40% / 50% / 60% / 100% | the federal schedule |
| `sai_contribution_rate` | `0.05` | the planning approximation |

## 4. Rules the numbers follow — do not "fix" these

- **A negative net worth is floored at zero.** A business worth less than it owes
  counts as nothing; it must never become a deduction against the family's other
  assets.
- **The schedule is marginal.** A business above the top threshold is not taxed at
  one flat rate: every band below still contributes its own line. The table shows
  every band the net worth reaches into, not just the last one.
- **A boundary belongs to the band below it.** At exactly 180,000 the first band
  is full and the second has not started.
- **`adjusted_from_table` duplicating `adjusted_net_worth` is deliberate.** One
  walks the bands and adds them up; the other applies `base + rate × (nw − floor)`
  to the landing band, which is how the federal table is printed. Every self-test
  asserts both. If someone edits a `base` out of step with its `floor`, the two
  disagree and the tests fail — that is the entire point of the redundancy.
- **The 5% line is an estimate and says so.** The real path runs parent assets
  through the AAI bracket table at an income-dependent effective rate. The flat
  rate is deliberate, labelled as a planning figure, and points readers at the
  full SAI calculator. Do not replace it with a "correct" formula here.

## 5. Self-tests

9 cases live in `window.__CALC_SELFTEST__` at the bottom of the formula script.
The **Checks** button on the Markup tab runs them; a published row is expected to
be 9/9 green. Keep them, and re-run after any edit.

| Case | What it locks |
|---|---|
| No business is no asset | zero in, zero out |
| A business worth less than it owes cannot become a deduction | the negative floor, `applicable_bracket: -1` |
| Inside the first band, 40 cents on the dollar | 100,000 → 40,000 |
| The first threshold is the next bracket's base amount | 180,000 → 72,000 |
| Second band adds 50% of the excess | 300,000 → 132,000 |
| Second threshold reaches the published 252,000 base | 540,000 → 252,000 |
| Third band adds 60% of the excess | 700,000 → 348,000 |
| Third threshold reaches the published 471,000 base | 905,000 → 471,000 |
| Above the top threshold the excess counts in full | 1M → 566,000, contribution 28,300 |

## 6. Editing checklist

Restyling or reordering:

1. Keep every attribute in §2 on some element, and keep `.cmm-calc` as an
   ancestor of all of them.
2. Keep the fragment a fragment — no document wrapper, no external stylesheet or
   font link, no `fetch`, no `localStorage`.
3. Keep both scripts, with their `id`s. `bnw-formula` is run on its own by the
   test harness; renaming it hides the calculator from the checks.
4. Put styling in the `<style>` block rather than inline attributes.
5. Run the Checks button. 9/9 before saving a published row.

Changing the numbers: bracket thresholds, bases and rates are Data-tab edits and
need no markup change. Two cautions — `base` is denormalised, so a `floor` moved
without its `base` breaks the §4 agreement; and this same federal schedule also
appears as `business_farm_brackets` in the `fafsa-sai-2027-28` config, so **update
both calculators together** and re-run both sets of checks.
