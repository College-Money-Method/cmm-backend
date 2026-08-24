# FAFSA 2027-28 SAI Calculator

- **Slug:** `fafsa-sai-2027-28` · **Type:** `fafsa_sai`
- **Embed:** `/embed/calculators/fafsa-sai-2027-28` · **On-site:** `/calculators/fafsa-sai-2027-28`
- **Source of truth:** the `calculators` row. The markup and config are
  authored in the frontend repo at `app/lib/calculators/fafsa-sai-2027-28/`; this
  brief is authored in the backend repo at
  `scripts/seed/calculator-documentation/fafsa-sai-2027-28.md`. Both are pushed into
  the row by `scripts/seed/seed_calculators.py`, and after seeding, CMS
  edits win until the next run.

Read this before changing anything here. It records where the numbers come from,
the contract the markup has to keep, and the cases that lock the arithmetic.

---

## 1. How it gets its data

**One rule: the markup never hard-codes a dollar figure.** Every yearly number is
read from `window.__CALC_CONFIG__`, so a policy update is a Data-tab edit, not a
deploy.

```
Postgres calculators row (html + config JSONB)
        │  GET /api/v1/calculators/public/fafsa-sai-2027-28   ← published only, no auth, no cookie
        ▼
buildCalculatorDocument()        app/lib/calculators/build-calculator-document.ts
  ├─ <head>: charset, viewport, robots=noindex, color-scheme=light
  ├─ iframeThemePreamble()       brand fonts + --cmm-* vars
  ├─ buildCalculatorDeps(config) <script>window.__CALC_CONFIG__={…}</script> + row.deps
  ├─ <style>  --calc-bg from ?bg=  (transparent by default)
  ├─ <body>   row.html VERBATIM, UNSANITIZED
  └─ resize script → postMessage {type:"cmm-calculator-resize", slug, height}
```

Four surfaces render that same document, so none can silently differ: the public
embed (`routes/embed/calculator-document.ts`), the on-site page
(`routes/calculators/$slug.tsx`), the CMS block
(`components/content/calculator-iframe.tsx`), and the admin preview
(`components/calculators/calculator-preview.tsx`, which also injects the
self-test harness).

Load-bearing serving details — do not "simplify" these away:

- **No cookie, token or session is read** on the embed routes. The response is
  publicly cacheable and a partner page must never be able to harvest one.
- **`Cache-Control: public, no-cache` + ETag** (`id-updated_at-bg`). With a
  `max-age`, an author's fix would keep rendering stale markup on every
  embedding page until the TTL expired.
- **`frame-ancestors` only when the allowlist is non-empty.** No allowlist ⇒ no
  header, i.e. any origin may frame it.

## 2. The contract the markup must keep

| Global | Direction | Rule |
|---|---|---|
| `window.__CALC_CONFIG__` | injected | the `config` bag verbatim |
| `window.__CALC_RUN__(inputs)` | authored | **pure** — no `document`, no `window.location`, no `fetch`, no `localStorage`. Returns one flat result object |
| `window.__CALC_SELFTEST__` | authored | `[{name, inputs, expect, tolerance?}]`. Numbers compare within `tolerance`, everything else strict `===` |

The formula lives in `<script id="sai-formula">` and is the only script CI
evaluates; the UI script beside it is the only part allowed to touch the
document. `authored-selftests.test.ts` extracts the formula by that id and runs
it in `node:vm` with nothing but `{window:{__CALC_CONFIG__:config}}` — so an
accidental `document.` reference in the formula fails CI, not just the page.

No PII leaves the browser: the form calls `preventDefault`, nothing is POSTed,
nothing is written to the URL.

**Inputs** (`__CALC_RUN__`): `filing_status`
(`married_joint` | `married_separate` | `single` | `head_of_household` |
`qualifying_widower` | `not_required`), `marital_status`, `family_size`,
`number_in_college`, `state_schedule` (`contiguous` | `alaska` | `hawaii`),
`parent1_earned`, `parent2_earned`, `parent_other_income`,
`parent_untaxed_income`, `parent_adjustments`, `parent_income_tax`,
`parent_cash`, `parent_investments`, `parent_business_farm`, `student_earned`,
`student_other_income`, `student_taxes_paid`, `student_assets`.

**Result fields:** `parent_agi`, `parent_total_income`, `parent_income_tax`,
`parent_oasdi_allowance`, `parent_medicare_allowance`, `parent_ipa`,
`parent_employment_expense_allowance`, `parent_total_allowances`, `pai`,
`business_farm_adjusted`, `parent_net_worth`, `pca`, `paai`, `pc`,
`student_total_income`, `student_negative_aai_allowance`,
`student_total_allowances`, `student_available_income`, `sci`, `sca`, `sai`,
`pell_award`, `pell_status` (`max` | `partial` | `min` | `none` | `ineligible` |
`unknown`), `poverty_guideline`, `max_pell_threshold`, `min_pell_threshold`,
`eligible_max_pell`, `eligible_min_pell`, `pell_awards_estimated`,
`pell_awards_pending`, `double_max_pell_gate_applied`, `not_required_to_file`.

## 3. Config — 32 keys, the largest bag

Rendered by the generic Data-tab form from the `fafsa_sai` descriptor list in
`app/lib/calculators/config-schema.ts`. Field kinds: `text`, `number`, `money`,
`percent` (stored as decimal `0.062`, shown as `6.2%`), `boolean`, `table`,
`group` (heading only).

| Group | Keys |
|---|---|
| Award year | `award_year`, `max_pell_award` 7395, `min_pell_award` 740, `pell_awards_estimated` true |
| Poverty guidelines | `poverty_guidelines[8]` `{persons, contiguous, alaska, hawaii}` + `poverty_increment_contiguous/alaska/hawaii` |
| Income protection | `ipa_dependent_student` 12220, `ipa_parent[5]` `{family_size, amount}`, `ipa_parent_increment` 7260 |
| Payroll tax | `oasdi_wage_base` 176100, `oasdi_rate` .062, `oasdi_max_per_earner`, `medicare_rate` .0145, `medicare_addl_rate` .0235, `medicare_addl_threshold_single/joint/separate` |
| Other allowances | `employment_expense_rate` .35, `employment_expense_cap` 5200, `apa` 0 |
| Assessment rates | `business_farm_brackets[4]` + `aai_brackets[6]`, both `{floor, base, rate}`; `parent_asset_rate` .12, `student_income_rate` .5, `student_asset_rate` .2 |
| Floors / Pell | `sai_floor` −1500, `pell_max_multiple_single_parent` 2.25, `pell_max_multiple_other` 1.75, `pell_min_multiple_single_parent` 3.25, `pell_min_multiple_other` 2.75 (multiples of the poverty guideline: 2.25 = 225%) |

Pell awards are flagged **estimated** because the 2027-28 federal figures are not
published yet — that is what `pell_awards_estimated` is for. When
`max_pell_award` is null the calculator states a notice and skips the packaging
step rather than guessing.

## 4. What it computes

AGI → allowances (payroll tax per earner, IPA by family size with the increment
past 6, employment expense) → available income; parent assets × 12% → AAI;
business/farm net worth walked through its marginal bracket table; AAI walked
through its own; student income × 50% and student assets × 20%, both floored at
zero; SAI floored at −1,500; then Pell packaging against the poverty multiples.

Both bracket tables use `base + rate × (x − floor)`, so a yearly update is the
same edit in the same two columns.

## 5. Deliberate divergences from the source workbook

14 of them. The workbook is wrong in each — do not "restore" any of these
without new evidence:

- Medicare 2.35% tier keyed to **each parent's own** earned income (F2), which is
  why there are **two separate parent earned-income inputs** (F3); MFS uses the
  125,000 threshold per parent.
- `Qualifying Widower` treated as **not married** → full earned income (F4).
- `Not required to file` **short-circuits to SAI = −1,500** before any allowance
  math (F5).
- IPA extends past family size 6 by +7,260/member (F7).
- **Student available income actually wired through** — the workbook hardcodes 0 (F15).
- Negative-parent-AAI allowance gated on the current IPA, not the stale 9,410 (F16).
- Student available income and student asset contribution both floor at 0 (F17, F18).
- Step-1 test is `0 < AGI ≤ threshold` — the `> 0` half was missing (F20).
- Max-Pell-eligible ⇒ max Pell regardless of SAI, and the index is capped at 0 (F21).
- Step-3 min-Pell comparison actually performed (F22).
- **SAI ≥ 2 × max Pell ⇒ Pell-ineligible** (statutory, effective 2026-07-01),
  skipped with a stated notice when `max_pell_award` is null (F23).

## 6. Tests

11 cases in `window.__CALC_SELFTEST__`, run in two places: the admin Markup
tab's **Checks** button, and CI via
`app/lib/calculators/__tests__/authored-selftests.test.ts` (auto-discovers any
directory holding both `calculator.html` and `config.json` — a new calculator
needs no wiring).

What each case pins down:

| Case | Locks |
|---|---|
| Non-filer parents get the statutory floor outright | `not_required` short-circuit (F5) |
| Max-Pell eligibility does not lift a floored index | F21 against a −1,500 index |
| Max-Pell eligibility caps a positive index at zero | F21's `min(0, sai)` cap |
| Middle-income joint filers, no student income | the main allowance → PAI → SAI path |
| A working student's own income raises the index | F15, the wired-through student path |
| Single parent above the surcharge threshold | Medicare tier at the single threshold |
| Separate returns measured against the separate threshold | F2/F3 per-parent keying |
| Household of seven extends the protection allowance | IPA increment past the table (F7) |
| Negative student net worth cannot reduce the index | `sca` floor (F18) |
| Business net worth discounted on the federal schedule | `business_farm_brackets` walk (566,000 at 1M) |
| A very high index removes Pell entirely | the 2 × max-Pell gate (F23) |

Verification history worth keeping: 11/11 green in the node harness, 11/11
against the served embed, and 0 divergences against an independent Python
reference written from the spec prose across every intermediate field. That
reference caught three self-test expectations authored wrong by hand — the JS was
right each time. **If a self-test and the formula disagree, suspect the test
first.**

## 7. Updating this calculator

1. Award-year figures — poverty guidelines and increments, IPA table and
   increment, OASDI wage base, Medicare thresholds, employment expense cap, both
   bracket tables, Pell max/min — are **all Data-tab edits**. No markup change,
   no deploy.
2. `max_pell_award` / `min_pell_award`: set them and clear
   `pell_awards_estimated` once the federal figures publish.
3. Bracket tables carry a denormalised `base` column. Moving a `floor` **without**
   its `base` is the classic silent break; the shape is `base + rate × (x − floor)`.
4. After any edit: run **Checks** in the Markup tab and expect all 11 green
   before saving a published row. The publish gate enforces this.
5. Adding or renaming a config key means editing the `fafsa_sai` descriptor list
   in `config-schema.ts` too, or the Data tab falls back to raw JSON.

## 8. Creating a new calculator from this pattern

1. Create the row in the admin UI (`/admin/calculators/new`) — always a draft.
   The `type` is fixed after creation because the markup reads config fields by
   name.
2. Add `"<new_type>"` to `CALCULATOR_TYPES` in **both**
   `app/types/calculators.ts` and `src/calculators/models.py`, plus a label in
   `CALCULATOR_TYPE_LABELS`.
3. Add a descriptor list for the type in `config-schema.ts`, otherwise the Data
   tab degrades to raw JSON.
4. Author `app/lib/calculators/<slug>/calculator.html` and `config.json`. The
   markup is a **fragment** — several `<script>` elements, no document wrapper.
   Name the formula script `id="<prefix>-formula"`; CI matches
   `<script id="[\w-]*formula">`.
5. Declare `__CALC_SELFTEST__` cases from day one — CI asserts every authored
   calculator declares at least one.
6. Call `preventDefault` on every form. They currently only avoid submitting
   because HTML skips implicit submission with 2+ blocking fields; one added
   button would turn every income field into a query parameter.
7. Write the brief at `scripts/seed/calculator-documentation/<slug>.md` in
   the backend repo — it is what the Documentation tab shows.
8. Register the slug in `CALCULATORS` in `scripts/seed/seed_calculators.py`,
   then seed with an explicit `--source` and `--env-file`.

## Open questions

1. Does the foreign-income exclusion added by the Aug 2025 revision of the
   2026-27 guide carry into 2027-28? Not modelled either way — matching the
   workbook. Needs a manual read of the FSA PDF (403s to automation).
2. Never verified with a super_admin session: inserting this calculator into a
   draft CMS page via the slash-command node, and running the checks through the
   admin `SelfTestRunner` UI.
