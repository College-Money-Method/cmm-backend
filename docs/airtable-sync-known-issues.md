# Airtable Sync — Current State & Resolution

**Status:** current-state reference for the Airtable→Supabase counselor sync. Reflects `main` as of 2026-07-14 (offboarding work merged + post-merge refinements + the nuke-and-resync launch strategy). Verify each claim at the cited line before relying on it; line numbers drift.

**Direction:** Airtable → Supabase, one-way. For **counselors** the sync now mirrors Airtable closely, including removals (add / update / deactivate / revoke). For **schools, cohorts, workshops, webinars** it is still *"add new + overwrite some fields"* — no deletion.

**Access rule (enforced):** to be *provisioned* a contact needs `email IS NOT NULL AND school_id IS NOT NULL AND deleted_at IS NULL` (`sync_provisioning.py:88-99`) **AND a role** (`contact.role`) — a contact with no role that was never provisioned is skipped entirely (no auth user created; `:139-144`, `:209-211`). Airtable is source-of-truth for existence + the label. Revocation is *soft* — deletes `UserRole` + `profiles`, KEEPS the Supabase auth user (reversible). `super_admin` never touched.

**Role vs. hub permission are DECOUPLED (`sync_provisioning.py:196-206`):** `contact.role` (Airtable) is the source of truth for the **display label** (`UserRole.school_role`, e.g. "Director"/"Counselor") only. The **hub permission** (`UserRole.role` = `hub_admin` / `hub_user`) is derived from the role **only when the role row is first created** (`Director → hub_admin`, else `hub_user`). Once the row exists the permission is never re-derived from Airtable — so **manual permission changes in the app survive re-syncs**. Re-syncs only update the label.

**Access signal note:** `softr_access` is the *legacy* Softr flag, NOT the new-hub access signal — do not use it for offboarding. Whether a contact has access is signalled by `contacts.user_id` being set (there is no `hub_access` DB column).

---

## Divergence strategy: nuke-and-resync (the current answer)

In-place reconciliation of a drifted contacts↔auth state proved too fragile, so the operational reset is a **clean wipe + rebuild from Airtable**.

**Script:** `scripts/backfill/nuke_and_resync_counselors.py`
**Run:** `uv run --env-file=.env.local python -m scripts.backfill.nuke_and_resync_counselors [--apply]` (dry-run without `--apply`).

With `--apply` (one DB txn, then auth deletes):
1. `DELETE FROM survey_responses` (avoids dangling `user_id` FKs)
2. `DELETE FROM sales` — ⚠️ **destructive side-effect**: sales rows FK contacts and are wiped too
3. `DELETE FROM user_roles WHERE role <> 'super_admin'`
4. `DELETE FROM profiles WHERE user_id NOT IN (:super_ids)`
5. `DELETE FROM contacts` (full wipe — rebuilt from Airtable)
6. commit
7. delete each non-super Supabase auth user
8. run `sync_schools_contacts_from_airtable` up to 3× until stable (`contacts_created == 0 AND contacts_deactivated == 0 AND counselors_created == 0`)

`super_admin` roles + their auth users are preserved throughout.

> **Implication:** because the reset path is a full rebuild, the sync no longer needs to perfectly reconcile every divergence in place. The "provisioning drift self-heal" logic was intentionally dropped (commit `84cf65b`).

---

## Pipeline order (`sync.py:36-39`)

`sync_schools_contacts_from_airtable` runs, in fixed order:
1. `sync_cohorts_from_airtable` (cohorts first, so schools resolve their cohort link)
2. `sync_schools_from_airtable`
3. `sync_contacts_from_airtable`
4. `provision_counselors_from_contacts` (provision active contacts + reconcile revocations)

---

## Resolution status

| Issue | Status | Current behavior (on `main`) |
|-------|--------|------------------------------|
| ISSUE-1 offboarding revoke | ✅ Resolved | `_reconcile_revocations` (`sync_provisioning.py:24-61`) revokes access when the backing contact is inactive. Gated by `SYNC_ENABLE_REVOKE` (default **False = log-only**). |
| ISSUE-2 empty email | ✅ Resolved | Emptying a set email is a no-op + warning (`sync_contacts.py:109-117`); access is tied to `school_id`, not email. |
| ISSUE-3 contacts deletion | ✅ Resolved | `contacts.deleted_at` (migration 0080). Contacts absent from the pull (matched by `airtable_id` only) are soft-deactivated; guarded by `sync_deactivation_max_missing_fraction` (**default 0.1 = 10%**). Reappearing `airtable_id` reactivates. |
| ISSUE-4 cohort chaining | ✅ Resolved | Cohort sync runs first in the pipeline; unresolved school→cohort links counted (`cohorts_unresolved`). |
| ISSUE-6 cohort upsert | ✅ Resolved | `hide_unavailability_calendar` upserted on match; `name` stays create-only (dedup key). |
| ISSUE-7 duplicate email | ✅ Resolved | **School-preferring** dedup — see below. Collisions logged; losers skipped. |
| ISSUE-3 schools/cohorts/workshops deletion | ⏭️ Deferred | Only contacts deactivate. These entity rows never delete. |
| ISSUE-3b webinar↔school mapping removal | ✅ Resolved | `_reconcile_portal_mappings` (`sync_webinars.py`) deletes `portal_mapping` rows Airtable no longer lists. Scoped to webinars with a **non-empty** Airtable `Schools` list; guarded by `sync_deactivation_max_missing_fraction` (default 0.1). |
| ISSUE-5 school write-once fields | ⏭️ Deferred | Out of scope. |
| ISSUE-8 reassignment unlink | ⏭️ Deferred / mostly mitigated | See below. |

---

## Resolved — implementation notes

### ISSUE-1 — Airtable-driven offboarding revoke
`_reconcile_revocations` (`sync_provisioning.py:24-61`) runs at the end of every provisioning pass. Revokes a role when `should_revoke_access` is true: `role != super_admin` AND `user_id ∈ managed` (has a backing contact) AND `user_id ∉ active` (active = `school_id NOT NULL AND deleted_at NULL AND user_id NOT NULL`). When revoking: `db.delete(role)` + `delete_profile`; the auth user is kept (reversible). Gated by `settings.sync_enable_revoke` (default `False`) — when off, logs `[log-only] WOULD revoke…` without acting.
Manual path also exists: `DELETE /api/v1/contacts/{user_id}` (`router.py:498-523`) hard-deletes the auth user, deletes the `UserRole` + `profiles`, and detaches the contact (`contact.user_id = None`, row kept).

### ISSUE-2 — Empty email is a no-op
`sync_contacts.py:109-117`: if an existing contact has an email and the Airtable record now sends empty, `effective_email = existing.email` (kept) + warning. Never blanks a previously-set email.

### ISSUE-3 — Contact soft-deactivation (contacts only)
`sync_contacts.py:188-215`: after upserting the pull, re-queries all contacts with a non-NULL `airtable_id`; any `airtable_id` in DB but absent from the pull gets `deleted_at = now()`. Match by `airtable_id` ONLY (never email). Guarded by `deactivation_is_safe` (`sync_utils.py:55-71`): skips deactivation + logs error if the pull is empty while contacts exist, or if `missing/known > sync_deactivation_max_missing_fraction` (default 0.1). Reappearance clears `deleted_at` (reactivated).

### ISSUE-4 — Cohorts chained first
Cohort sync is step 1 of the pipeline (`sync.py:36`). Schools count unresolved cohort links (`cohorts_unresolved`).

### ISSUE-6 — Cohort upsert
`cycles/sync.py:16-87`: on match, backfills `airtable_id` and updates `hide_unavailability_calendar`. `name` is create-only.

### ISSUE-7 — Duplicate email → school-preferring dedup
Changed from the earlier first-occurrence rule (commit `2462328`). `pick_collision_skip_ids` (`sync_utils.py:40-52`): when several Airtable records share an email in one pull, the **winner is the first record with a non-empty `Sch` (school link)**; if none have one, the first occurrence wins. All other same-email records are skipped (`sync_contacts.py:90-93`) and logged with record IDs. Separately, the DB-side `contact_by_email` map prefers a row with `user_id` set when two DB rows share an email (`sync_contacts.py:72-77`). Contact match order: `airtable_id` → global lowercased `email`.
Duplicate-login guard (`sync_provisioning.py:170-180`): if a contact resolves to an auth user already claimed by another contact, it is **skipped entirely** (`skipped += 1; continue`) — no role synced. `claimed_user_ids` is seeded from all contacts first (`:119-124`).

---

## Still open / deferred

### ISSUE-3 (non-contacts) — Schools / cohorts / workshops never delete
Records removed from Airtable persist forever for these entity rows. Only contacts deactivate. Deferred on purpose.
- [ ] Decide per-entity soft-delete/active-flag policy. Must reuse the partial-fetch guard so a bad pull can't mass-delete.

**Resolved sub-case — webinar↔school mappings (ISSUE-3b):** removing a school from a webinar's `Schools` list in Airtable now deletes the corresponding `portal_mapping` on the next sync. `_reconcile_portal_mappings` runs post-loop, scoped to webinars whose Airtable `Schools` list is **non-empty** (an empty list is treated as "unmanaged / lookup glitch", never as "wipe all mappings"), and is protected by the same `sync_deactivation_max_missing_fraction` guard (skips + logs `reconciliation SKIPPED` if the stale fraction exceeds the threshold). Known limitation: removing the *last* school from a webinar (Airtable `Schools` → empty) is not reconciled — remove it via `DELETE /webinars/{id}/schools/{school_id}` if needed.

### ISSUE-5 — School descriptive fields are write-once [MEDIUM]
`sync_schools.py` update branch refreshes only `airtable_id`, `is_current_customer`, `cohort_id`, `airtable_slug`. `name`, address, and URL fields are set on create and never updated — Airtable edits to them are ignored.
- [ ] Document field ownership; update Airtable-owned fields on existing schools (keep `slug` app-owned for URL stability).

### ISSUE-8 — Reassignment to an un-synced school unlinks [LOW]
If a contact's `Sch` points to a school not yet in Supabase, `school_id` → NULL. Under the access rule this now means access is (log-)revoked until the school syncs. Because cohorts+schools sync before contacts in one run, a full sync self-corrects; still silent per-contact when it happens.
- [ ] Log unresolved `Sch` links per-contact; decide retain-vs-unlink.

---

## Key files

- `src/schools/sync.py` — orchestrator (cohorts → schools → contacts → provisioning)
- `src/schools/sync_contacts.py` — contact upsert, empty-email no-op, collision skip, soft-deactivation
- `src/schools/sync_provisioning.py` — auth user + `UserRole` + `profiles` provisioning; `_reconcile_revocations`
- `src/schools/sync_schools.py` — school upsert (write-once descriptive fields)
- `src/cycles/sync.py` — cohort create + `hide_unavailability_calendar` upsert
- `src/schools/sync_utils.py` — `pick_collision_skip_ids`, `deactivation_is_safe`, `should_revoke_access`, `parse_bool/int`
- `src/auth/profile_sync.py` — `upsert_profile` / `delete_profile` for the `profiles` mirror table
- `src/auth/router.py` — contact CRUD incl. `DELETE /api/v1/contacts/{user_id}` (renamed from `/counselors`, commit `dbe0360`)
- `scripts/backfill/nuke_and_resync_counselors.py` — clean wipe + rebuild
- migrations: `0079_add_profiles_table.py`, `0080_add_deleted_at_to_contacts.py`

**Config keys:** `sync_enable_revoke` (default `False`, log-only), `sync_deactivation_max_missing_fraction` (default `0.1`).

**`profiles` table:** mirrors Supabase `auth.users` (email + name) for fast counselor search. Kept in sync by `upsert_profile` in provisioning (`sync_provisioning.py:186`, every pass) and cleaned by `delete_profile` in revoke + the DELETE endpoint.

---

## Cross-cutting invariants

- **Idempotency:** syncs are safe to re-run; per-record failures are isolated (savepoints) and don't abort the batch.
- **Partial-fetch safety:** deactivation is skipped when the Airtable pull looks empty/partial (`deactivation_is_safe`).
- **`super_admin`:** never revoked, deactivated, or demoted by any sync path or the nuke script.

## Unresolved questions

1. Turn on live revocation: flip `SYNC_ENABLE_REVOKE=true` after reviewing first-run log-only output. When?
2. Deletion policy for schools/cohorts/workshops/webinars (ISSUE-3 non-contacts) — intentional to leave, or implement later?
3. Which school fields should Airtable own vs. app own? (ISSUE-5)
4. Nuke script deletes `sales` rows (FK to contacts) — is that acceptable data loss, or should sales be re-keyed/preserved?
5. `airtable_asset_base_id` is configured but unused by any sync — legacy or planned?
