# FAFSA Student Aid Index estimator (2027-28)

- **Slug:** `fafsa-sai-2027-28` · **Type:** `fafsa_sai`
- **Embed:** `/embed/calculators/fafsa-sai-2027-28` · **On-site:** `/calculators/fafsa-sai-2027-28`
- **Where it lives:** the `calculators` database row — the markup in the Markup
  tab, all 32 federal figures in the Data tab. Editing the row changes the live
  calculator.

**If you are an AI agent editing this calculator:** you have been given the
markup. This is the largest and most wired of the four calculators — a five-step
form whose step list changes with the answers. Read §2, §5 and §7 before you change
it. Styling, copy and layout are yours to change freely; the attributes in §2 are
the wiring, and removing one stops the calculator dead. Nothing in §4 is a bug to
fix, however wrong it looks against a federal worksheet.

---

## 1. What it does

Estimates the Student Aid Index for a **dependent** student (federal Formula A):
parent income less allowances, plus 12% of parent assets, run through the AAI
bracket table; the student's own income at 50% and assets at 20%; the total
floored at −1,500. Then it packages a Pell Grant estimate against the federal
poverty guideline for that household.

Five panels, in DOM order — heading, progress bar, step chips, then the form:

| `data-panel` | Step | Holds |
|---|---|---|
| 0 | Family | state, family size, number in college, marital status, filing status |
| 1 | Parent income | two earned-income fields, other/untaxed income, adjustments, tax paid |
| 2 | Assets | cash, investments, business or farm net worth |
| 3 | Student | student earned/other income, tax paid, assets |
| 4 | Results | the index, the Pell sentence, notices, and a collapsible breakdown table |

One panel is visible at a time — `data-active="true"`, set by the script.
**The step list is not fixed:** choosing *Not required to file* collapses it to
two steps (family → results), because that answer short-circuits the whole
calculation (§4).

## 2. How the file is wired

The markup is a **fragment** — a `<style>` block, the HTML, then two `<script>`
elements. It is injected into a document that is built around it, so it must
never contain `<html>`, `<head>`, `<body>` or `<!doctype>`. Brand fonts and the
`--cmm-*` colour variables are already in that document; do not add a font or
stylesheet link.

| Part | Role |
|---|---|
| `<style>` | all styling, scoped by `.cmm-calc` and `.calc-*` classes |
| `<div class="cmm-calc">` | the root. The UI script gives up silently without it |
| `<script id="sai-formula">` | the math. Pure — no DOM, no network, no storage |
| `<script id="sai-ui">` | the stepper, the form reader, the result painter |

Markup and logic are joined by attributes, not by structure — so you can move,
rewrap and restyle anything as long as the attributes travel with it:

| Attribute | Meaning |
|---|---|
| `data-calc-form` | the form the UI script listens to |
| `name="<field>"` | the 18 inputs of §3. Read by `form.elements[name]` |
| `id="f-state"` | the state `<select>`. Its options are **generated** — 53 of them |
| `data-panel="0…4"` | one step each. The count defines which panel is "results" |
| `data-active` | which panel is showing — styling hook, script-set |
| `data-next` / `data-prev` | any number of them; each moves one step |
| `data-steps` | the chip strip. Rebuilt on every step change; chips carry `data-step-to` |
| `data-progress-bar` / `data-progress-label` | the bar's width and the "Step 2 of 5" text |
| `data-marital="married" \| "single" \| "both"` | on each filing-status `<option>`: which marital answers it survives |
| `data-parent2` | the wrapper hidden for a single parent |
| `data-text="heading_income" \| "heading_assets" \| "parent1"` | slots reworded between "Parent's" and "Parents'" |
| `data-out="<field>"` | the five result targets |
| `data-copy-link` / `data-reset` | the two result-panel buttons |

Only **five** `data-out` targets, unlike the other calculators — the results
panel is mostly generated:

| `data-out` | Painted with |
|---|---|
| `award_year` | config, twice: once at startup, once per render |
| `sai` | the index, or `—` |
| `pell_summary` | a full sentence of HTML, branching on `pell_status` |
| `notes` | the notices — Pell figures unpublished, estimate caveats |
| `breakdown` | the `<tbody>` of the "How this number was built" table, ~20 generated `<tr>` |

Things that bite:

- `data-out` is looked up with `querySelector`, so **only the first match is
  painted.** To show a figure twice, add a second name in the UI script.
- All five are painted unconditionally. **Delete the element and the paint
  throws**, which freezes the results panel. Hide it with CSS instead.
- `data-steps`, `data-progress-bar`, `data-progress-label`, `data-parent2`,
  `data-copy-link` and `data-reset` are each looked up **once at startup**.
  Removing any one of them throws before the first panel ever renders.
- `data-panel` numbering is load-bearing: the **last** panel is the results
  panel, and the values are read as numbers. Renumber them together or not at
  all.
- The chip strip's contents are `innerHTML`-replaced on every step change. Never
  hand-write a chip inside it.
- Breakdown rows are generated by the UI script's `row(label, value, class)`
  helper — the row labels are in the script, not the markup. Restyle them via
  `.calc-sub` and `.calc-total`.

Config arrives as `window.__CALC_CONFIG__` before these scripts run. The form
calls `preventDefault` — **keep that.** With 18 fields nothing submits by
accident today, but one added submit button would turn every income figure into a
query string. The only thing that may ever put a family's numbers in a URL is the
reader clicking **Copy a link**, and the caption beside that button says so.

## 3. Inputs, results, config

**Inputs** — 18. Five selects plus thirteen money fields:

| Group | Fields |
|---|---|
| Household | `state_schedule` (`contiguous` \| `alaska` \| `hawaii`, derived from the state select), `family_size`, `number_in_college`, `marital_status`, `filing_status` (`married_joint` \| `married_separate` \| `single` \| `head_of_household` \| `qualifying_widower` \| `not_required`) |
| Parent income | `parent1_earned`, `parent2_earned`, `parent_other_income`, `parent_untaxed_income`, `parent_adjustments`, `parent_income_tax` |
| Parent assets | `parent_cash`, `parent_investments`, `parent_business_farm` |
| Student | `student_earned`, `student_other_income`, `student_taxes_paid`, `student_assets` |

**Results:** `parent_agi`, `parent_total_income`, `parent_income_tax`,
`parent_oasdi_allowance`, `parent_medicare_allowance`, `parent_ipa`,
`parent_employment_expense_allowance`, `parent_total_allowances`, `pai`,
`business_farm_adjusted`, `parent_net_worth`, `pca`, `paai`, `pc`,
`student_total_income`, `student_negative_aai_allowance`,
`student_total_allowances`, `student_available_income`, `sci`, `sca`, `sai`,
`pell_award`, `pell_status` (`max` \| `partial` \| `min` \| `none` \|
`ineligible` \| `unknown`), `poverty_guideline`, `max_pell_threshold`,
`min_pell_threshold`, `eligible_max_pell`, `eligible_min_pell`,
`pell_awards_estimated`, `pell_awards_pending`, `double_max_pell_gate_applied`,
`not_required_to_file`.

**Config — 32 keys**, the largest bag. All Data-tab edits, no deploy: the markup
hard-codes no dollar figure.

| Group | Keys |
|---|---|
| Award year | `award_year`, `max_pell_award` 7395, `min_pell_award` 740, `pell_awards_estimated` true |
| Poverty guidelines | `poverty_guidelines[8]` `{persons, contiguous, alaska, hawaii}` + `poverty_increment_contiguous/alaska/hawaii` |
| Income protection | `ipa_dependent_student` 12220, `ipa_parent[5]` `{family_size, amount}`, `ipa_parent_increment` 7260 |
| Payroll tax | `oasdi_wage_base` 176100, `oasdi_rate` .062, `oasdi_max_per_earner`, `medicare_rate` .0145, `medicare_addl_rate` .0235, `medicare_addl_threshold_single/joint/separate` |
| Other allowances | `employment_expense_rate` .35, `employment_expense_cap` 5200, `apa` 0 |
| Assessment rates | `business_farm_brackets[4]`, `aai_brackets[6]` — both `{floor, base, rate}`; `parent_asset_rate` .12, `student_income_rate` .5, `student_asset_rate` .2 |
| Floors / Pell | `sai_floor` −1500, `pell_max_multiple_single_parent` 2.25, `pell_max_multiple_other` 1.75, `pell_min_multiple_single_parent` 3.25, `pell_min_multiple_other` 2.75 — multiples of the poverty guideline, so 2.25 = 225% |

Both bracket tables evaluate as `base + rate × (amount − floor)`, and below the
first floor return that first bracket's `base` — which is what gives the AAI
table its flat −1,958 tier without a special case.

## 4. Rules the numbers follow — do not "fix" these

Each of these looks like a bug against some published worksheet. Each is
deliberate and self-tested:

- **Two separate parent earned-income fields exist because the Medicare
  surcharge is keyed to each parent's own earnings**, not to the couple's
  combined figure. Married-filing-separately measures each parent against the
  125,000 threshold. Do not merge the fields.
- **"Qualifying surviving spouse" is treated as not married** — full earned
  income, one parent.
- **"Not required to file" short-circuits to SAI = −1,500** before any allowance
  math runs, and collapses the form to two steps. It is not an allowance case.
- **The parent IPA continues past family size 6** at +7,260 per additional
  member, rather than stopping at the printed table's last row.
- **The student's own income is wired through to the index.** Worksheets that
  hardcode a zero here are wrong; a working student's earnings do move the
  number.
- **Student available income and the student asset contribution both floor at
  0.** Negative student net worth can never reduce the family's index.
- **Step 1 of Pell is `0 < AGI ≤ threshold`** — a zero AGI does not pass it.
- **Max-Pell eligibility grants max Pell regardless of the index, and the index
  itself is capped at 0** in that case. A floored −1,500 index stays −1,500.
- **SAI ≥ 2 × max Pell removes Pell entirely** — statutory, effective
  2026-07-01. When `max_pell_award` is null the gate is skipped and a notice says
  so, rather than being guessed at.
- **Pell amounts are labelled "estimated"** while `pell_awards_estimated` is
  true, because the 2027-28 federal figures are not published. If
  `max_pell_award` is null the calculator states that and skips packaging
  entirely instead of inventing a number.
- **Only Alaska and Hawaii get their own poverty schedule.** Territories and
  "outside the 50 states" correctly use the 48-state column.
- **Hiding the second parent's field also zeroes it.** A wage typed before
  switching to a single parent must not keep counting from behind a hidden input.

## 5. Translation

The site is read in Spanish and Chinese as well as English, and this calculator
has to follow. It cannot be translated the way the rest of a page is: the embed
is **its own browsing context**, and the site's translation engine walks the
page's DOM — a walk that stops dead at an iframe boundary. So the document is
translated on the server before it is sent, whenever the URL carries
`?tl=<locale>`. The site adds that automatically; the whole mechanism is
`/embed/calculators/fafsa-sai-2027-28?tl=es`, which is also how you
check your work.

That timing is what constrains the markup:

> **Only text that is already in the markup can be translated.** A string built
> from a JavaScript literal is invisible to the translator and stays English in
> every language.

### The string bank

Every string the script writes into the page lives as a text node in a hidden
bank, the last child of `.cmm-calc`:

```html
<div data-strings style="display:none" aria-hidden="true">
  <span data-k="some_key">The sentence a reader sees.</span>
</div>
```

`display:none` does not exempt it — the skip rules deliberately ignore
visibility, which is exactly what makes the pattern work. The `sai-ui`
script reads a string back with the `S(key, tokens)` helper defined at the top
of it, which fills the `{token}` slots:

```js
S("some_key", { amount: money(total) })
```

Token filling uses `split`/`join`, not `String.replace` — a money value starts
with `$`, and `$&` in a replacement string is a substitution pattern rather than
text. Do not "simplify" that to `.replace()`.

### Rules for anyone editing the markup

1. **New reader-facing string → new bank entry.** Never type display text into
   the script. This includes `<option>` labels, table row labels, headings built
   at runtime, button text, and messages.
2. **Keep a sentence whole.** One entry is one complete sentence with `{token}`
   slots, never fragments joined in script. Translated word order differs from
   English, and a fragment cannot be reordered.
3. **Do not split a sentence with inline markup.** `<strong>` inside a sentence
   cuts it into separate translation units, each translated out of context. Style
   the whole element instead, or put the emphasised part in its own element
   beside the sentence.
4. **Prefer static markup to generated markup.** A `<select>` written out with
   its `<option>`s, or a table with its labels written out, is translated for
   free and needs no bank entry. Build in script only what genuinely varies.
5. **Numbers stay numbers.** Figures come from `money()` at runtime and are never
   translated. Codes, years and rates are left alone too — a string with no
   letters is skipped.
6. **Opting out.** `<script>` and `<style>` content is never translated, and
   brand terms are protected automatically. To hold something else in English,
   mark it `translate="no"`, `class="notranslate"` or `data-no-translate`.

Config prose is translated too. Every string value in the Data tab that contains
a letter is sent for translation — question wording, category labels, notes — so
those may be written as plain display prose. Values with no letters (an award
year, a rate) are left exactly as typed.

### This calculator's bank

Twenty-five entries, the largest bank of the four:

| Key | English |
|---|---|
| `step_1` … `step_5` | Family · Parent income · Assets · Student · Results |
| `step_chip` | {number}. {label} |
| `step_progress` | Step {number} of {total} |
| `married_heading_income` | Parents' income |
| `married_heading_assets` | Parents' assets |
| `married_parent1` | Parent 1 earned income (from work) |
| `single_heading_income` | Parent's income |
| `single_heading_assets` | Parent's assets |
| `single_parent1` | Parent's earned income (from work) |
| `estimated_award` | estimated award |
| `pell_pending` | Pell award amounts for {year} have not been published yet. |
| `pell_this_year` | this year |
| `pell_max` | Eligible for the maximum Pell Grant, about {amount} a year. |
| `pell_partial` | Estimated Pell Grant of about {amount} a year. |
| `pell_min` | Eligible for the minimum Pell Grant, about {amount} a year. |
| `pell_ineligible` | Not eligible for a Pell Grant: the index is at least twice the maximum award. |
| `pell_none` | Not eligible for a Pell Grant based on these figures. |
| `note_not_required` | Because the parents are not required to file a federal return, … |
| `note_pending` | The Student Aid Index above is complete. … |
| `note_multiple` | Having {count} students in college no longer divides … |
| `link_copied` | Link copied |

The `married_*` / `single_*` pairs are read as `S(scope + "_" + key)` against the
`[data-text]` slots, so both halves of every pair must exist.

Two parts of this calculator were moved out of the script and into the markup for
the same reason, and must stay there:

- **the state `<select>`** — every `<option>` written out rather than built in
  script. The schedule only ever depends on the code, so the visible name is free to be
  translated.
- **the breakdown table** — every row written out with its label and a
  `<td data-out="row_*">` for the figure. Rows carry `data-row="full"` or
  `data-row="statutory"`; the script only toggles `hidden` between the two sets
  and fills the figures.

### Checking it

Open `/embed/calculators/fafsa-sai-2027-28?tl=es` and read it.
Everything a reader sees should be Spanish; anything still in English is a
string that escaped the bank.

## 6. Self-tests

11 cases live in `window.__CALC_SELFTEST__` at the bottom of the formula script.
The **Checks** button on the Markup tab runs them, and the publish gate enforces
them: a published row is expected to be 11/11 green.

| Case | What it locks |
|---|---|
| Non-filer parents get the statutory floor outright | the `not_required` short-circuit |
| Max-Pell eligibility does not lift a floored index | −1,500 stays −1,500 |
| Max-Pell eligibility caps a positive index at zero | the `min(0, sai)` cap |
| Middle-income joint filers, no student income | the main allowance → PAI → SAI path |
| A working student's own income raises the index | the wired-through student path |
| Single parent above the surcharge threshold | Medicare tier at the single threshold |
| Separate returns measured against the separate threshold | per-parent surcharge keying |
| Household of seven extends the protection allowance | the IPA increment past the table |
| Negative student net worth cannot reduce the index | the `sca` floor |
| Business net worth discounted on the federal schedule | the `business_farm_brackets` walk — 566,000 at 1M |
| A very high index removes Pell entirely | the 2 × max-Pell gate |

This formula was checked field by field against an independent implementation of
the federal spec, with zero divergences. That pass found three self-test
expectations that had been typed wrong by hand — the formula was right every
time. **If a self-test and the formula disagree here, suspect the test first.**

## 7. Editing checklist

Restyling or reordering:

1. Keep every attribute in §2 on some element, and keep `.cmm-calc` as an
   ancestor of all of them. The six startup lookups are the ones that fail
   loudest.
2. Keep the fragment a fragment — no document wrapper, no external stylesheet or
   font link, no `fetch`, no `localStorage`.
3. Keep the `[data-strings]` bank and the `S()` helper, and keep display text
   in the bank rather than in the script (§5).
4. Keep both scripts, with their `id`s. `sai-formula` is run on its own by the
   test harness; renaming it hides the calculator from the checks.
5. Keep `preventDefault` on the form, and keep the caption under **Copy a link**
   if you keep the button (§2).
6. Moving a field between panels is safe — the script reads by `name`, not by
   panel. Renumbering panels is not: the last one is the results panel.
7. Generated content — state options, step chips, breakdown rows, the Pell
   sentence — can only be restyled through CSS and through the UI script. There
   is no template for them in the markup.
8. Test both marital answers and *Not required to file* after any change to the
   step flow; that path drops three panels.
9. Run the Checks button. 11/11 before saving a published row.

Changing the numbers: the whole yearly update — poverty guidelines and
increments, the IPA table and its increment, the OASDI wage base, Medicare
thresholds, the employment-expense cap, both bracket tables, Pell max and min —
is a Data-tab edit. Three cautions:

- Bracket tables carry a **denormalised `base`** column. Moving a `floor`
  without its `base` is the classic silent break.
- `business_farm_brackets` is the **same federal schedule** as the standalone
  `business-net-worth` calculator. Update both together and re-run both sets of
  checks.
- Set `max_pell_award` / `min_pell_award` and clear `pell_awards_estimated`
  together, once the federal figures publish.
