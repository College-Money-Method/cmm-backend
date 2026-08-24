# Assets Calculator for Applications

- **Slug:** `application-assets` · **Type:** `application_assets`
- **Embed:** `/embed/calculators/application-assets` · **On-site:** `/calculators/application-assets`
- **Source of truth:** the `calculators` row. The markup and config are
  authored in the frontend repo at `app/lib/calculators/application-assets/`; this
  brief is authored in the backend repo at
  `scripts/seed/calculator-documentation/application-assets.md`. Both are pushed into
  the row by `scripts/seed/seed_calculators.py`, and after seeding, CMS
  edits win until the next run.

Read this before changing anything here.

---

## 1. How it gets its data

**One rule: the markup never hard-codes a figure or a piece of application
wording.** Everything variable is read from `window.__CALC_CONFIG__`, so a
yearly update is a Data-tab edit, not a deploy. This calculator holds **no rates
at all** — what drifts here is the two applications' question wording, and that
is exactly what the config carries.

```
Postgres calculators row (html + config JSONB)
        │  GET /api/v1/calculators/public/application-assets   ← published only, no auth, no cookie
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

The formula lives in `<script id="assets-formula">` and is the only script CI
evaluates, in `node:vm` with nothing but `{window:{__CALC_CONFIG__:config}}`. The
UI script beside it is the only part allowed to touch the document. The form
calls `preventDefault`; nothing is POSTed and no PII leaves the browser.

**Inputs:** `investments[]` (one figure per configured category),
`student_529`, `siblings[]` `{amount, owner: "sibling" | "parent", age_19_plus}`,
`properties[]` `{market, debt}`.

**Result fields:** `investment_accounts_total`, `student_529`,
`sibling_529_css_total`, `siblings[]` `{amount, counted_fafsa, counted_css}`,
`education_total_fafsa`, `education_total_css`, `properties[]` `{equity}`,
`property_equity_total`, `fafsa_total`, `css_total`, `fafsa_question`,
`css_question`.

Every figure is carried through as a **pair** rather than computed once and
adjusted — the two applications ask for different totals from the same accounts,
and pairing them is what keeps the two columns honest.

## 3. Config — 5 keys, no rates

| Key | What it is |
|---|---|
| `fafsa_question` | the FAFSA investment line's verbatim wording |
| `css_question` | the CSS Profile's verbatim wording |
| `investment_categories[6]` | `{label, note}` — the six account types counted in full |
| `excluded_categories[4]` | `{label}` — the "leave these out" list |
| `award_year` | label only, nothing computes from it |

`excluded_categories` is `{label}` rows rather than bare strings so the existing
`table` field kind renders it — no new `ConfigField` kind was needed. Keep that
shape if you add to it.

## 4. What it computes

```
FAFSA = investments + student 529 + property equity (each property floored at 0)
CSS   = investments + all reportable 529s
```

The rules that differ:

| Item | FAFSA | CSS Profile |
|---|---|---|
| Investment accounts (6 categories) | counted | counted |
| Student's 529 | counted, whoever owns it | counted |
| Sibling's 529 | never | counted **unless** sibling-owned *and* sibling is 19+ |
| Second property | equity, floored at 0 | excluded — its own section |

Two floors matter: a property worth less than is owed on it is reported as 0,
never as a negative that would quietly shrink the rest of the family's reported
assets. And a sibling's plan drops off CSS only when it is **both** owned by that
sibling **and** theirs as an adult — parent-owned, or sibling-owned under 19, and
it stays a family asset.

## 5. Deliberate divergences from the source worksheet

- The sheet asks "is this 529 owned by the Student or Parent?" (C19) and then
  never references the answer — a dependent student's 529 is a parent asset
  either way. **The web version drops a question that cannot change the answer.**
  Do not add it back.
- The sibling verdict strings (C27/C31/C35) all say "Sibling 1" from a
  copy-paste; the UI generates per-row text.
- Columns H–J are spreadsheet chrome (row-visibility toggles) and carry no
  policy; the web version adds and removes rows instead.

## 6. Tests

9 cases in `window.__CALC_SELFTEST__`, run by the admin Markup tab's **Checks**
button and by CI (`app/lib/calculators/__tests__/authored-selftests.test.ts`,
which auto-discovers any directory holding both `calculator.html` and
`config.json`).

| Case | Locks |
|---|---|
| An empty worksheet reports nothing | zero in, zero out |
| Investment accounts count in full on both applications | the shared investment line |
| The student's own 529 counts on both | ownership is irrelevant for the student's plan |
| A sibling's own 529 drops off both once they turn 19 | the one case a 529 disappears entirely |
| A sibling's own 529 still counts on CSS while under 19 | the age half of the two-part test |
| A parent-owned sibling 529 counts on CSS whatever the age | the ownership half |
| Property equity is market less debt, on FAFSA only | the FAFSA/CSS property split |
| A property worth less than it owes is reported as zero | the per-property floor, not a netted negative |
| A full worksheet lands on two different totals | 260,000 FAFSA vs 100,000 CSS end to end |

The three sibling cases are the heart of it: that four-way rule is the only real
logic in this calculator, and each branch has its own case.

## 7. Updating this calculator

1. Both applications' question wording, the six investment categories and their
   notes, and the excluded list are **all Data-tab edits**. That wording is
   exactly the part that drifts year to year — no markup change, no deploy.
2. `investment_categories` length drives the number of input rows; `inputs.investments`
   is positional, so **adding a category mid-list shifts every self-test's
   `investments` array**. Append rather than insert, or update the cases.
3. Expect 9/9 green before saving a published row; the publish gate enforces it.
4. Adding or renaming a config key means editing the `application_assets`
   descriptor list in `config-schema.ts` too, or the Data tab falls back to raw
   JSON.

## 8. Creating a new calculator from this pattern

1. Create the row at `/admin/calculators/new` — always a draft. `type` is fixed
   after creation because the markup reads config fields by name.
2. Add the new type to `CALCULATOR_TYPES` in **both** `app/types/calculators.ts`
   and `src/calculators/models.py`, plus a label in `CALCULATOR_TYPE_LABELS`.
3. Add a descriptor list for the type in `config-schema.ts`. Prefer reusing the
   existing field kinds — `table` of `{label}` rows beats inventing a
   string-array kind, as `excluded_categories` shows.
4. Author `app/lib/calculators/<slug>/calculator.html` and `config.json`. The
   markup is a **fragment** — several `<script>` elements, no document wrapper.
   Name the formula script `id="<prefix>-formula"`; CI matches
   `<script id="[\w-]*formula">`.
5. Declare `__CALC_SELFTEST__` cases from day one, one per branch of any
   conditional rule — CI asserts every authored calculator declares at least one.
6. Call `preventDefault` on every form.
7. Write the brief at `scripts/seed/calculator-documentation/<slug>.md` in
   the backend repo — it is what the Documentation tab shows.
8. Register the slug in `CALCULATORS` in `scripts/seed/seed_calculators.py`,
   then seed with an explicit `--source` and `--env-file`.

## Open questions

1. `award_year` is still `2023-24`, from the worksheet filename. Nothing computes
   from it but the label is visible — confirm or update.
2. The "leave these out" list (retirement accounts, primary home, life insurance
   cash value, personal possessions) was authored here, not taken from the
   worksheet. Editable in the Data tab; worth confirming the wording.
