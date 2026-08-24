# Assets calculator for applications

- **Slug:** `application-assets` · **Type:** `application_assets`
- **Embed:** `/embed/calculators/application-assets` · **On-site:** `/calculators/application-assets`
- **Where it lives:** the `calculators` database row — the markup in the Markup
  tab, the wording and categories in the Data tab. Editing the row changes the
  live calculator.

**If you are an AI agent editing this calculator:** you have been given the
markup. Read §2 and §6 before you change it. Styling, copy and layout are yours
to change freely; the attributes in §2 are the wiring, and removing one stops the
calculator dead. Nothing in §4 is a bug to fix.

---

## 1. What it does

The FAFSA and the CSS Profile ask for investment totals in different ways, so a
family filling in both needs two different numbers from the same accounts. This
calculator collects the accounts once and reports both totals side by side, with
each application's question quoted verbatim above its figure.

The visible surface, in DOM order: heading, the investment-category grid, the
"leave these out" list, the student's 529 field, an add-your-own list of siblings'
529s, an add-your-own list of properties, then the two result columns.

Unlike the other calculators here, this one holds **no rates at all**. What it
encodes is the two applications' rules about *which* accounts count.

## 2. How the file is wired

The markup is a **fragment** — a `<style>` block, the HTML, then two `<script>`
elements. It is injected into a document that is built around it, so it must
never contain `<html>`, `<head>`, `<body>` or `<!doctype>`.

| Part | Role |
|---|---|
| `<style>` | all styling, scoped by `.cmm-calc` and `.calc-*` classes |
| `<div class="cmm-calc">` | the root. The UI script gives up silently without it |
| `<script id="assets-formula">` | the rules. Pure — no DOM, no network, no storage |
| `<script id="assets-ui">` | builds the rows, calls the formula, paints the results |

Markup and logic are joined by attributes, not by structure — so you can move,
rewrap and restyle anything as long as the attributes travel with it:

| Attribute | Meaning |
|---|---|
| `data-calc-form` | the form the UI script listens to |
| `data-out="<field>"` | an element the UI script writes a result into |
| `data-investments` | container the category fields are generated into |
| `data-excluded` | list the "leave these out" items are generated into |
| `data-siblings` / `data-properties` | containers the repeatable rows are generated into |
| `data-add-sibling` / `data-add-property` | the buttons that append a row |
| `name="student_529"` | the one statically-named field |

Most of this page is **generated**, not authored: the six category inputs, the
excluded list, and every sibling and property row are built by the UI script from
config and from clicks. Style them by class; do not expect to find them in the
markup, and do not hand-write rows inside those containers — a re-render clears
them.

Two things about `data-out` that bite:

- The script looks it up with `querySelector`, so **only the first match is
  painted.** That is why several figures exist under two names — `fafsa_total` and
  `fafsa_total_row`, `education_total_css` and `education_total_css_row` — one for
  the table row, one for the summary. To show a figure in a third place, add
  another name in the UI script rather than duplicating an existing one.
- Every name in §3 is painted unconditionally. **Delete the element and the paint
  throws**, which kills the whole render — the totals freeze and typing stops
  doing anything. Hide it with CSS if you don't want it seen.

The two add-row buttons are looked up the same way. Removing one throws at
startup, before anything renders.

Config arrives as `window.__CALC_CONFIG__` before these scripts run. The form
calls `preventDefault`, so a submit button is safe to add; nothing is ever sent
anywhere, and no figure a family types may reach a URL, a log or a request.

## 3. Inputs, results, config

**Inputs** to `window.__CALC_RUN__`:

| Input | Shape |
|---|---|
| `investments[]` | one figure per configured category, **positional** |
| `student_529` | number |
| `siblings[]` | `{amount, owner: "sibling" \| "parent", age_19_plus}` |
| `properties[]` | `{market, debt}` |

**Results:** `investment_accounts_total`, `student_529`, `sibling_529_css_total`,
`siblings[]` `{amount, counted_fafsa, counted_css}`, `education_total_fafsa`,
`education_total_css`, `properties[]` `{equity}`, `property_equity_total`,
`fafsa_total`, `css_total`, `fafsa_question`, `css_question`.

Every figure is carried as a **pair** — a FAFSA value and a CSS value — rather
than computed once and adjusted. That pairing is what keeps the two columns from
drifting into each other.

**Config — 5 keys**, all Data-tab edits, no deploy:

| Key | What it is |
|---|---|
| `fafsa_question` | the FAFSA investment line's verbatim wording |
| `css_question` | the CSS Profile's verbatim wording |
| `investment_categories[6]` | `{label, note}` — the account types counted in full |
| `excluded_categories[4]` | `{label}` — the "leave these out" list |
| `award_year` | label only, nothing computes from it |

## 4. Rules the numbers follow — do not "fix" these

```
FAFSA = investments + student 529 + property equity (each property floored at 0)
CSS   = investments + all reportable 529s
```

| Item | FAFSA | CSS Profile |
|---|---|---|
| Investment accounts | counted | counted |
| Student's own 529 | counted, whoever owns it | counted |
| A sibling's 529 | never | counted **unless** sibling-owned *and* 19+ |
| Second property | equity, floored at 0 | excluded — its own section |

- **A property worth less than is owed on it reports as 0**, per property. Never
  as a negative that would quietly shrink the rest of the family's assets.
- **A sibling's plan drops off CSS only when both halves are true** — owned by that
  sibling *and* theirs as an adult. Parent-owned, or sibling-owned under 19, and
  it stays a family asset. This four-way rule is the only real logic here.
- **The student's 529 does not ask who owns it, deliberately.** For a dependent
  student it is a parent asset either way, so the question cannot change the
  answer. Do not add an owner selector to that field.

## 5. Self-tests

9 cases live in `window.__CALC_SELFTEST__` at the bottom of the formula script.
The **Checks** button on the Markup tab runs them; a published row is expected to
be 9/9 green. Keep them, and re-run after any edit.

| Case | What it locks |
|---|---|
| An empty worksheet reports nothing | zero in, zero out |
| Investment accounts count in full on both applications | the shared investment line |
| The student's own 529 counts on both | ownership is irrelevant for the student's plan |
| A sibling's own 529 drops off both once they turn 19 | the one case a 529 disappears entirely |
| A sibling's own 529 still counts on CSS while under 19 | the age half of the two-part test |
| A parent-owned sibling 529 counts on CSS whatever the age | the ownership half |
| Property equity is market less debt, on FAFSA only | the FAFSA/CSS property split |
| A property worth less than it owes is reported as zero | the per-property floor |
| A full worksheet lands on two different totals | 260,000 FAFSA vs 100,000 CSS, end to end |

## 6. Editing checklist

Restyling or reordering:

1. Keep every attribute in §2 on some element, and keep `.cmm-calc` as an
   ancestor of all of them.
2. Keep the fragment a fragment — no document wrapper, no external stylesheet or
   font link, no `fetch`, no `localStorage`.
3. Keep both scripts, with their `id`s. `assets-formula` is run on its own by the
   test harness; renaming it hides the calculator from the checks.
4. Generated rows can only be restyled through CSS and through the UI script's
   `createElement` calls — there is no row template in the markup to edit.
5. Run the Checks button. 9/9 before saving a published row.

Changing the content: both applications' question wording, the categories and
their notes, and the excluded list are all Data-tab edits — and that wording is
exactly what drifts year to year. One caution: `inputs.investments` is
**positional**, so inserting a category mid-list shifts every self-test's array.
Append instead, or update the cases.
