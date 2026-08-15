# Phase 2 (Opt-in) Implementation Report

## Executed Phase
- Phase: phase-02-opt-in
- Plan: /Users/nnavu/WebstormProjects/cmm-frontend/plans/260814-2028-automation-email-sender/
- Status: completed

## Files Modified

### Backend (cmm-backend)
- `src/auth/schemas.py` — `auto_emails: bool | None = None` on `ContactUpdate`; `auto_emails: bool = False` on `ContactOut`.
- `src/auth/router.py` — `_contact_out` passes `contact.auto_emails`; `_contact_out_from_role` defaults `auto_emails=False`; `update_contact` adds (a) 403 guard rejecting `body.role is not None` for non-`hub_admin`/`super_admin`, (b) self-edit branch applying `auto_emails` when `contact.user_id == user.user_id`, independent of role.
- `src/schools/sync_contacts.py` — removed `auto_emails` from `new_values` (Airtable no longer overwrites the counselor's own opt-in choice).
- `src/config.py` — added `unsubscribe_secret_key` (falls back to `supabase_service_role_key`).
- `src/main.py` — registered `emails_unsubscribe_router` alongside `emails_webhook_router` (additive only, no other Phase 1 changes).

### Backend — new
- `src/emails/unsubscribe.py` — HMAC-SHA256 signed/expiring token helpers (no `itsdangerous` dep; not in `pyproject.toml`).
- `src/emails/unsubscribe_router.py` — public `GET /api/v1/emails/unsubscribe?token=...`, no auth dependency, flips `Contact.auto_emails=False` + inserts an `EmailSuppression` row, generic response (no PII leak).
- `tests/emails/test_unsubscribe.py` — 10 tests (token round-trip, forgery, expiry, malformed, HTTP endpoint incl. idempotency + unknown-contact).
- `tests/auth/__init__.py`, `tests/auth/test_contact_auto_emails_self_edit.py` — 9 tests (self-edit success for hub_user/viewer, cannot edit others' auto_emails, role-field-present self-edit rejected 403, self-edit doesn't unlock name fields, hub_admin role self-change regression).
- `tests/schools/test_sync_contacts_auto_emails.py` — 2 tests (new contact ignores Airtable `auto_emails=True`; existing opt-in survives a re-sync).

### Frontend (cmm-frontend)
- `app/types/auth.ts` — added `auto_emails` to `ContactUpdate` (optional) and `ContactOut` (required, matches backend always-returned field). Not in the phase's listed file-ownership but required for typecheck to pass once components reference the field — additive only.
- `app/routes/hub/team/index.tsx` — loader unchanged (already returned `myUserId`); action's top-level admin gate relaxed from "any submission requires admin" to "only `create` intent requires admin" (required for self-edit to reach the action at all — the backend independently enforces per-field scope); update branch forwards `auto_emails` from formData when present; `TeamPage` destructures `myUserId`, computes per-row `canEditRow = canEdit || counselor.user_id === myUserId` and passes `viewerIsAdmin={canEdit}` to the dialog.
- `app/components/hub/edit-team-member-dialog.tsx` — added "Opt-In to Automated Emails" `Checkbox` (hidden-input-backed, same pattern as `grade-selector.tsx`); Hub Permission `Select` (and its `memberRole` hidden input) now render only when `viewerIsAdmin` — hidden entirely for a non-admin self-edit rather than disabled.

## Tasks Completed
- [x] `auto_emails` on `ContactUpdate`/`ContactOut`
- [x] `_contact_out` / `_contact_out_from_role` pass-through
- [x] Self-edit authz branch in `update_contact` + non-admin role-change 403
- [x] Remove Airtable `auto_emails` sync
- [x] Unsubscribe token + endpoint + tests
- [x] Self-edit authz tests
- [x] Frontend `myUserId` wiring + per-row `canEditRow`
- [x] Checkbox in `EditTeamMemberDialog` + role Select hidden for non-admin self-edit
- [x] Team action forwards `auto_emails`

## Tests Status
- Backend required set: `pytest tests/emails/test_unsubscribe.py tests/auth/test_contact_auto_emails_self_edit.py tests/schools/test_sync_contacts_auto_emails.py -q` → **21 passed** (confirmed stable across 3 repeat runs).
- `import src.main` → OK (only pre-existing unrelated `RequestsDependencyWarning`).
- Full backend suite (`uv run pytest -q`, excluding one pre-existing unrelated collection error in `tests/analytics/test_resource_breakdown_queries.py` — `ImportError: cannot import name 'resolve_video_rows'`, untouched by this work): 276 passed, 2 pre-existing failures in `tests/analytics/test_analytics_endpoints.py` (`other_videos` breakdown key), also unrelated — confirmed via `git status`/`git diff --stat` that no analytics file was touched this session.
- Frontend `pnpm typecheck`: no errors in any of the 3 modified files. One pre-existing error remains in `app/lib/tiptap/__tests__/email-render-golden.test.ts` (untracked Phase 1 test fixture, `GoldenCase` type mismatch) — confirmed pre-existing by `git stash` + rerunning typecheck with this session's diff removed, error persisted identically.
- No existing team-page-specific test files found in the frontend repo to run.

## Issues Encountered
1. **Blocking bug found + fixed in test fixtures (not app code)**: SQLite applies NUMERIC column affinity to `sqlalchemy.dialects.postgresql.UUID(as_uuid=True)` columns (`UserRole.user_id`, `UserRole.school_id`); an all-digit-repeated UUID hex string (e.g. `66666666-6666-...`) parses as a valid decimal integer and gets silently coerced to a float on write, corrupting the round-trip back to `uuid.UUID` on read (`AttributeError: 'float' object has no attribute 'replace'`). Confirmed via isolated repro (fails with repeated-digit UUIDs, passes with `uuid.uuid4()` or letter-only UUIDs). Fixed by changing `tests/auth/test_contact_auto_emails_self_edit.py`'s UUID constants to letter-only hex (`aaaaaaaa-...`, `bbbbbbbb-...`, etc.). No production code was affected — real Supabase-issued UUIDs are never all-digit-repeated.
2. **Flaky forged-signature tests**: the "flip the last character" corruption method sometimes lands on base64 tail padding-insensitive bits (when the encoded length mod 4 is 2 or 3), leaving the decoded byte unchanged and the forged token still verifying. Fixed by corrupting a character 6 positions from the end (safely inside a full base64 group), confirmed deterministic across repeated runs and mixed test-file ordering.
3. **Frontend authorization flow required going beyond the phase's literal line-number-scoped instructions**: the original action had a blanket admin-only gate before intent branching, which would have made self-edit unreachable. Relaxed the gate to admin-only for `intent === "create"`; self-edit fields are still fully scoped server-side (defense in depth, per phase's resolved design decision).
4. **`memberRole` hidden input**: previously always rendered (even for non-admin), which would have sent the counselor's own unchanged role back to the server and tripped the new 403 guard. Now the hidden input (and the Select) render only when `viewerIsAdmin`.

## Unsubscribe URL Format
```
{settings.app_public_url}/api/v1/emails/unsubscribe?token={base64url(contact_id:expiry_epoch:hmac_sha256_hex)}
```
Built by `src/emails/unsubscribe.py::build_unsubscribe_url(contact_id)`. Token TTL defaults to 1 year (`DEFAULT_TTL_SECONDS`) since footer links in old emails must keep working. Needed for Phase 1 email footer wiring: call `build_unsubscribe_url(contact.id)` and drop the returned URL into the template.

## Next Steps
- Unblocks Phase 3 (audience opt-in filter reads `auto_emails`) and Phase 5 (hard opt-in filter before scheduled sends).
- Phase 1's email footer template should call `build_unsubscribe_url` (not yet wired into any template — out of this phase's scope).
- No commit/push performed per constraints.

## Unresolved Questions
None — all constraints and acceptance criteria satisfied.

Status: DONE
