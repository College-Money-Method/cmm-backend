# College Aid phase 3: import + edit endpoints

## Files (src/colleges)
admin_schemas.py, import_rules.py (190), import_row.py (extra: ParsedRow/parse_row, kept rules < 200), import_issues.py (extra: capped IssueLog), import_diff.py (190), import_service.py, admin_router.py (`router`), admin_edit_router.py (`router`), edit_service.py (187).
Tests (tests/colleges): admin_helpers.py (CSV builder + client fixtures), test_import_rules.py (13), test_import_diff.py (15), test_admin_router.py (10, incl. real-CSV test, skipped unless env COLLEGE_AID_REAL_CSV set), test_admin_edit_router.py (11).
Result: `uv run pytest tests/colleges` 124 passed (with real CSV), 1 skipped without env. ruff not installed in venv: not run.

## Real CSV smoke (463 rows)
Dry run: 0 errors, 463 colleges/records created, warnings capped 100 + truncated, nothing written. Commit OK; re-import: 463 unchanged, 0 new colleges.

## Decisions / deviations
- REGION_BY_STATE: 50 + DC + PR. Matches CSV for all 46 states present. Guessed (not in CSV): AK West Coast, WY Rocky Mts, SD Plains, WV Southeast, PR Southeast. Please confirm.
- Blank `region` cell accepted (derived from state); non-blank must match.
- `dry_run` defaults to true. Commit sha mismatch/missing -> 422 ImportSummary with error column `expected_sha256` (flat body).
- Warnings: important ones (manual-edit overwrite, renames, older-release) are listed before routine ones so they survive the 100 cap. Per-row "new college" warnings kept (no collapsed count line), truncated=true covers it.
- POST /{id}/records returns 201. Edit 422 uses FastAPI shape `detail:[{loc:["body",field],msg,type}]`.
- Edit payload numbers/strings both accepted for numeric fields; explicit null clears (coa_estimated null -> false; release_label null -> 422). Patching with no change does not set manually_edited_*.
- csv.field_size_limit(10_000) is process-global (stdlib limitation), set at each import.
- Import release_label on a refreshed record is only rewritten when values change.
- Hidden colleges included in admin list and csv export (stable ids).
- Routers use sync handlers; non-matching `format` -> 422.

## Open questions
- Confirm the 5 guessed state regions above.
- src/main.py wiring: include admin_router.router before/after admin_edit_router.router (order irrelevant, verified).

**Status:** DONE_WITH_CONCERNS
**Summary:** Import (dry run/commit, sha-bound), list/csv export, and college/record edit endpoints implemented with tests; 124 pass incl. real-CSV smoke.
**Concerns/Blockers:** ruff not installed so lint unchecked; 5 state-to-region guesses (AK, WY, SD, WV, PR) need confirmation; main.py wiring left to you.
