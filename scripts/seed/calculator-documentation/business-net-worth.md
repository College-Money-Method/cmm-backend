# Net Worth of a Business

- **Slug:** `business-net-worth` · **Type:** `business_net_worth`
- **Embed:** `/embed/calculators/business-net-worth` · **On-site:** `/calculators/business-net-worth`
- **Source of truth:** the `calculators` row. The markup and config are
  authored in the frontend repo at `app/lib/calculators/business-net-worth/`; this
  brief is authored in the backend repo at
  `scripts/seed/calculator-documentation/business-net-worth.md`. Both are pushed into
  the row by `scripts/seed/seed_calculators.py`, and after seeding, CMS
  edits win until the next run.

Read this before changing anything here.

---

## 1. How it gets its data

**One rule: the markup never hard-codes a dollar figure.** Every yearly number is
read from `window.__CALC_CONFIG__`, so a policy update is a Data-tab edit, not a
deploy.

```
Postgres calculators row (html + config JSONB)
        │  GET /api/v1/calculators/public/business-net-worth   ← published only, no auth, no cookie
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

The formula lives in `<script id="bnw-formula">` and is the only script CI
evaluates, in `node:vm` with nothing but `{window:{__CALC_CONFIG__:config}}`. The
UI script beside it is the only part allowed to touch the document. The form
calls `preventDefault`; nothing is POSTed and no PII leaves the browser.

**Input:** `net_worth` — one number.

**Result fields:** `net_worth` (floored at 0), `brackets[]`
`{floor, ceiling, rate, amount}`, `applicable_bracket` (index, `-1` when none),
`adjusted_net_worth`, `adjusted_from_table`, `sai_contribution`,
`sai_contribution_rate`.

## 3. Config — 3 keys

| Key | Value | Notes |
|---|---|---|
| `award_year` | `"2025-26"` | label only, nothing computes from it |
| `brackets[4]` | `{floor, base, rate}` — 40% / 50% / 60% / 100% | byte-identical to `business_farm_brackets` in the `fafsa_sai` config |
| `sai_contribution_rate` | `0.05` | the planning approximation, see §5 |

## 4. What it computes

One input walked through the marginal brackets → adjusted net worth → × 5% → an
estimated SAI contribution.

A negative net worth is floored at zero before the walk: the published table
starts at $1, and letting a business in the red run through the 40% band would
turn a struggling business into a discount on everything else the family owns.

A second output, `adjusted_from_table`, computes `base + rate × (nw − floor)` for
the landing bracket — the published table's own form of the same schedule — and a
self-test asserts the two agree. That is the guard: a `base` column edited out of
step with the thresholds fails CI instead of quietly disagreeing with the printed
table.

## 5. Deliberate divergences from the source spreadsheet

- The prose table in column G prints `$170001` and `$510000`, contradicting its
  own formulas. **The formulas won.**
- The bracket shape was made byte-identical to `business_farm_brackets` in the
  SAI config and cross-checked against the SAI calculator's own self-test
  (566,000 at nw = 1M).

## 6. Tests

9 cases in `window.__CALC_SELFTEST__`, run by the admin Markup tab's **Checks**
button and by CI (`app/lib/calculators/__tests__/authored-selftests.test.ts`,
which auto-discovers any directory holding both `calculator.html` and
`config.json`).

| Case | Locks |
|---|---|
| No business is no asset | zero in, zero out |
| A business worth less than it owes cannot become a deduction | the negative floor, `applicable_bracket: -1` |
| Inside the first band, 40 cents on the dollar | 100,000 → 40,000 |
| The first threshold is the next bracket's base amount | 180,000 → 72,000; boundary belongs to the band below |
| Second band adds 50% of the excess | 300,000 → 132,000 |
| Second threshold reaches the published 252,000 base | 540,000 → 252,000 |
| Third band adds 60% of the excess | 700,000 → 348,000 |
| Third threshold reaches the published 471,000 base | 905,000 → 471,000 |
| Above the top threshold the excess counts in full | 1M → 566,000, contribution 28,300 |

Every case asserts `adjusted_net_worth` **and** `adjusted_from_table` together —
that pairing is the whole point (see §4).

## 7. Updating this calculator

1. Bracket thresholds, bases and rates are Data-tab edits. No markup change, no
   deploy.
2. `base` is denormalised. Moving a `floor` without its `base` is the classic
   silent break — here it fails CI, because `adjusted_from_table` stops agreeing
   with the band-by-band walk. Fix the config, not the test.
3. These brackets and `fafsa_sai`'s `business_farm_brackets` are the same federal
   schedule. **Update both**, and re-run both calculators' checks.
4. Expect 9/9 green before saving a published row; the publish gate enforces it.
5. Adding or renaming a config key means editing the `business_net_worth`
   descriptor list in `config-schema.ts` too, or the Data tab falls back to raw
   JSON.

## 8. Creating a new calculator from this pattern

1. Create the row at `/admin/calculators/new` — always a draft. `type` is fixed
   after creation because the markup reads config fields by name.
2. Add the new type to `CALCULATOR_TYPES` in **both** `app/types/calculators.ts`
   and `src/calculators/models.py`, plus a label in `CALCULATOR_TYPE_LABELS`.
3. Add a descriptor list for the type in `config-schema.ts`.
4. Author `app/lib/calculators/<slug>/calculator.html` and `config.json`. The
   markup is a **fragment** — several `<script>` elements, no document wrapper.
   Name the formula script `id="<prefix>-formula"`; CI matches
   `<script id="[\w-]*formula">`.
5. Declare `__CALC_SELFTEST__` cases from day one — CI asserts every authored
   calculator declares at least one. Where a figure exists in two forms (a walk
   and a published table), assert both, as this one does.
6. Call `preventDefault` on every form.
7. Write the brief at `scripts/seed/calculator-documentation/<slug>.md` in
   the backend repo — it is what the Documentation tab shows.
8. Register the slug in `CALCULATORS` in `scripts/seed/seed_calculators.py`,
   then seed with an explicit `--source` and `--env-file`.

## Open questions

1. `D22 = D19 × 0.05` is a planning approximation, not the federal formula — the
   real path runs parent assets at 12% into AAI and then through the AAI bracket
   table (effective ~2.6–5.6%, income-dependent). This calculator follows the
   spreadsheet, labels the number an estimate, and points at the full SAI
   calculator. **Keep the flat 5% line, or drop the number for a link-out?**
