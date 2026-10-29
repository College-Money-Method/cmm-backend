# College Aid CSV Contract

Every yearly aid file is converted into this one CSV shape before it is
imported via `POST /api/v1/colleges/admin/import`. The import re-validates every
rule below; the converter (`scripts/colleges/convert_cmm_aid_workbook.py`) only
produces contract-shaped data.

## File

- UTF-8 (a BOM is accepted), comma separated, one header row, ≤ 5,000 rows, ≤ 2 MB.
- Header set is exact: every column below, no extras. Order does not matter.
- One `release_label` for the whole file.
- An empty cell means **not reported**. `N/R` / `NR` are accepted and read as empty.
  On import an empty cell never erases a value already stored.

## Columns

| Column | Type / allowed values | Workbook header (Sept-2026) | CDS line |
|--------|----------------------|-----------------------------|----------|
| `college_id` | empty or uuid of an existing college | – | – |
| `name` | text, required | Institution | – |
| `state` | USPS code, 2 letters, required | State | – |
| `region` | one of the 8 CMM regions; must match `state` | Region | – |
| `control` | `public` \| `private` | Public/ Private | – |
| `coa_in_state` | money, publics only | 2025-26 Estimated Total Cost of Attendance (In-State) | G (in-district/in-state) |
| `coa_out_of_state` | money (privates: the COA) | 2025-26 Total Estimated Cost of Attendance (Out-of-State) | G |
| `coa_year` | `YYYY-YY`; required when any COA is present | – (workbook footnote 5) | – |
| `coa_estimated` | `true` \| `false` | – (footnote 5: 2024-25 COA × 1.04) | – |
| `full_time_undergrads` | non-negative integer | Full-Time Undergrads | B |
| `aid_application` | text, e.g. `FAFSA`, `CSS Profile`, `Princeton Form` | Application for Institutional Aid | H5/H6 |
| `pct_need_met` | decimal fraction `0..1` | Average Percent of Need Met | H2 line I |
| `pct_non_need_receiving_merit` | decimal fraction `0..1` | Percent of Non-Need Undergrads Receiving Merit | H2A line N ÷ B |
| `avg_merit_award` | money | Average Merit Award to Non-Need Undergrads | H2A line O |
| `cds_year` | empty (not reported) or `YYYY-YY`, second part = first + 1 | CDS Year | – |
| `source` | text | Source | – |
| `release_label` | `YYYY-MM`, same on every row | – | – |
| `data_notes` | text | – | – |

Value formats:

- **Money**: plain number, `$` and `,` stripped, stored to cents.
- **Percent**: fraction only (`0.85`, not `85` or `85%`), stored to 4 decimals.
- **Years**: `YYYY-YY` (`YYYY-YYYY` also accepted on import). The first year must be within 2000 and next year. Excel dates are rejected.

Not columns: "% need unmet", "% non-need paying full COA" (= 1 − `pct_non_need_receiving_merit`)
and "merit % of COA" are computed at read time.

## College matching

- `college_id` set → that college (must exist). A name change is a warning.
- `college_id` empty → matched by `(normalize_name(name), state)`. No match → a **new
  college** is created (listed as a warning in the dry run so renames get caught).
- `normalize_name` (`src/colleges/name_key.py`): lowercase, `&` → `and`, drop
  punctuation, collapse spaces. `"St. John's College"` → `st johns college`.
- Changing `state` or `control` of an existing college is an error unless the
  import is run with `allow_identity_change=true`.

## Records and versioning

- One record per (college, `cds_year`). Each college versions on its own: a
  college missing from a new file keeps showing its older year.
- Same `cds_year` in a newer release updates that record. A release older than
  the one stored is an error unless `allow_older_release=true`.
- Empty `cds_year` → the college's single "year not reported" record.
- A record an admin corrected by hand is never overwritten silently: every value
  the file would change shows up as an `overwrites manual edit` warning.

## Keeping ids stable across years

Download `college_id,name,state` from the admin College Aid page (or
`GET /api/v1/colleges/admin?format=csv`) and pass it to the converter as
`--colleges-export`. For a renamed school add a row to
`scripts/colleges/college_aliases.csv` (`source_name,state,college_id,note`).

## Converting a workbook

The workbook is untrusted input: keep it outside the repo and run Python isolated.

```bash
uv run python -I scripts/colleges/convert_cmm_aid_workbook.py /path/to/aid.xlsx \
  --release 2026-09 --out /path/outside/repo [--colleges-export colleges-export.csv]
```

Outputs `college-aid-<release>.csv` (sorted by state, name; byte-identical on
re-run) and `review-warnings.csv` (outliers, trimmed whitespace, N/R years) for a
human to review before import. A workbook with a new layout needs a small adapter
that writes this same contract.
