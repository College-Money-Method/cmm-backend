# Airtable Sync — Contact↔Auth Reconciliation Plan

**Status:** investigation + plan. Prerequisite for enabling Airtable-driven offboarding (revoke). Do NOT enable `SYNC_ENABLE_REVOKE` until this is done.

**Context:** While validating the offboarding work (see `airtable-sync-known-issues.md`), a deeper data-integrity problem surfaced: many `contacts.user_id` links point at the *wrong* Supabase auth login. Offboarding/revocation is unsafe until these links are trustworthy.

---

## Problem

`contacts.user_id` should point to the `auth.users` row whose email equals `contacts.email`. In practice many don't.

**Evidence (production, read-only, 2026-07-14):**
- Contacts with a `user_id`: **468**
- Auth email **matches** contact email: **416**
- Auth email **mismatch** (wrong login): **52**
- Of the 52 mismatches, **49** are *active* (school-linked + hold a `user_role`) → real access impact.
- Local dev DB returned identical numbers → it is a faithful copy of prod.

## Root cause

An Airtable contact record's `Email` gets changed or reused for a different person. `sync_contacts` updates `contacts.email` (Airtable = source of truth), **but provisioning never re-links `contacts.user_id`** when the email changes — because it only resolves a login when `user_id` is NULL. So the contact keeps pointing at the previous person's login.

Example (Allison Bly): live row `recJL5` has `contacts.email = ably@carondeleths.org` but `user_id` → auth login `cstarks@sfprep.org`. The record was previously cstarks, its email was changed to Allison's, the login link never followed.

This compounds with duplicate rows: the same email exists on 2 rows (one live `airtable_id`, one dead), each with its own `user_id`.

## Data characterization (the 52 mismatches)

| Metric | Count | Meaning |
|---|---|---|
| A correct login exists (auth email = contact email) | **51 / 52** | Re-linkable — the right login is already in `auth.users` |
| …and that correct login holds a role | **51 / 52** | — |
| The wrong login has a rightful-owner contact | **41 / 52** | Links are swapped between two real people |
| No correct login exists | **1 / 52** | Needs re-provision or manual handling |

Conclusion: this is a mostly-mechanical **re-link by email** plus untangling a permutation of swapped links — not data loss.

## Related latent bugs (found during offboarding validation)

1. **Stale `airtable_id`s** — Airtable deletes+recreates records (new id); sync matched by email but never refreshed the stored id. Fixed on branch `feat/airtable-sync-offboarding` (refresh id on email-match). Reduced would-be false offboards 364 → 76.
2. **Duplicate contact rows** — 43 email groups have >1 row (dead-id twin + live-id twin). ~76 rows are unclaimed by any live Airtable record.
3. **Duplicate auth users** — the swapped links mean some people effectively have two logins.

---

## Plan

### Phase 0 — Clean baseline
- Restore the **local** DB from a fresh prod dump. (Exploration on 2026-07-14 mutated local: 332 `airtable_id` refreshes, 376 field updates, 3 auth users created; `deleted_at` was reverted, no roles deleted.)
- Land the `airtable_id`-refresh fix first (already implemented + tested) so stale ids stop accumulating.

### Phase 1 — Re-link contacts to the correct login (read → dry-run → apply)
- For each contact, target `user_id` = the `auth.users` row whose `lower(email) = lower(contacts.email)`.
- **Dry-run first**: produce a report of every proposed change `(contact_id, email, old_user_id→new_user_id)` and the 1 unresolved case. Review before applying.
- Apply as one transaction; handle the `uq_contacts_user_id` unique constraint by resolving the full permutation (e.g. null out all affected `user_id`s, then set targets), so swapped pairs don't collide mid-update.
- Keep the profiles mirror (`profiles`) consistent with the new links.
- The 1 no-correct-login contact → re-provision a login from its current email (or flag for manual review).

### Phase 2 — De-duplicate contact rows
- Per email, keep the row whose `airtable_id` is present in the current Airtable pull (the live record); retire the dead-id twin.
- Decide the retirement mechanism: soft (`deleted_at`) vs merge-then-delete. Ensure the surviving row carries the correct `user_id` from Phase 1.
- Reconcile the now-orphaned duplicate auth users (delete vs keep-disabled) — product decision.

### Phase 3 — Provisioning hardening (prevent recurrence)
- On sync, when `contacts.email` changes such that it no longer matches the linked `auth.users.email`, **re-link** `user_id` to the login matching the new email (create if none), instead of leaving the stale link.
- Add a sync-time report/counter for `contact.email ≠ auth.email` so drift is visible.

### Phase 4 — Enable offboarding
- Only after Phases 1–2 verify 0 (or fully-understood) mismatches: run offboarding in log-only, review the (now small) revoke list, then set `SYNC_ENABLE_REVOKE=true`. Keep the 10% deactivation guard.

---

## Safety principles
- Every mutating phase: dry-run report → human review → apply in a transaction.
- Never delete a Supabase auth user without an explicit, reviewed decision (soft-disable preferred).
- `super_admin` never touched.
- Guard against partial Airtable fetches (existing `deactivation_is_safe`).

## Decisions (2026-07-14)
- **Duplicate row retirement:** soft-delete (`deleted_at`), release `user_id`. ✅
- **Orphaned duplicate login** (person's stale 2nd account): revoke its `UserRole` + `profiles` row, **keep** the Supabase auth user. Never touch `super_admin` or admin-created (still-referenced) logins. ✅
- **Email reuse:** treated as human error (accidental delete+recreate → new `airtable_id`), NOT legitimate reuse. Root remedy = refresh `airtable_id` on email-match (done) + relink by email.
- **`carolinglee@gmail.com`** (1 no-login case): test user, left as-is (keeps existing login).

## Status
- Phase 1 (relink) + Phase 2 (dup soft-delete) + orphan-role revoke: implemented in
  `scripts/backfill/reconcile_contact_auth_links.py` (dry-run default, `--apply`).
- **Validated on local**: mismatches 52 → 1, 44 dup rows soft-deleted, Allison correct.
  (Orphan-revoke code added after that run; needs a clean baseline to fully exercise.)
- Prod untouched.

## Phase 3 — provisioning hardening (DONE)
`sync_provisioning.py`: before resolving a contact's login, if the linked
`auth.users.email` no longer matches `contacts.email`, drop the stale link so it
re-resolves to the correct login (create if none). Surfaced as `drift_relinked`
in the sync result. Prevents the cross-wiring from recurring.
- Edge case: the `carolinglee@gmail.com` test row (linked to a real person's
  login) WILL be self-healed on next sync — a new `carolinglee@gmail.com` login
  is created and the real person's login is freed. Acceptable (frees a squatted
  login); hard-delete the test row later if unwanted.

## Remaining steps
1. Restore local from a fresh prod dump (Phase 0), run the reconcile script dry-run → `--apply`, verify 0 real mismatches + orphan roles revoked.
2. Run reconcile on **prod** after a DB backup (dry-run → apply).
3. Then enable offboarding: log-only → review → `SYNC_ENABLE_REVOKE=true`, 10% guard intact.

## Open (minor)
- Phase 3 detail: auto-reprovision vs flag when an email change has no existing login.
- Optionally correct `auth.users.user_metadata` (name) from contact during relink.
