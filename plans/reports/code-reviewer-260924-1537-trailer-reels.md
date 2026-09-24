# Trailer reels — production-readiness review

Scope: backend `src/video_pipeline/reel_*.py`, `task_dispatch.dispatch_reel`, `archive_original.py`,
`zoom_recording_fetch.py`, `process_recording.py`, `trailer_select.py`, migration 0130; frontend
`video-pipeline.ts`, `$jobId.tsx`, `video-pipeline-reels*.tsx`, `video-pipeline-reels-display.ts`;
infra `modules/ecs-task/main.tf`. Read-only, no edits made.

## Critical

None found — auth (`AdminDep`/`require_admin`, `src/auth/deps.py:61`), S3 IAM scoping, and
ContentType are all correct (see Positive Observations).

## High

### 1. Reel task's terminal writes don't guard against a superseded state — a false "stale"
timeout gets silently resurrected to `ready`, and the "one reel at a time" invariant breaks

`src/video_pipeline/reel_task.py:71-74` (success) and `:100-103` (failure) write
`reel.state = READY` / `FAILED` unconditionally, with no check that the row is still the one the
task itself moved to `RENDERING`. Compare with the reference pattern in `run_task.py:130-133`,
which explicitly re-checks `job.job_state in (PROCESSING, PENDING)` before writing a terminal
state — precisely to avoid clobbering a state some other actor already moved on. `reel_task.py`
drops that guard.

Concrete scenario:
1. Reel R is `RENDERING`. The ECS task is genuinely still working (Fargate cold start + Bedrock
   throttling + a slow Transcribe queue — plausible under AWS degradation, not just a hung task).
2. An admin loads/polls the job page → `reel_service.list_reels` → `_expire_stale`
   (`reel_service.py:53-59`) sees `updated_at` older than `STALE_AFTER` (60 min) and marks R
   `FAILED` with `STALE_ERROR`, commits.
3. R is no longer in `ACTIVE_STATES`, so `create_reel` (`reel_service.py:81`) now happily accepts
   a second request for the same job → a second ECS task launches, defeating the "a job renders
   one reel at a time" contract the module's own docstring promises.
4. The original (still-running) task eventually finishes and calls
   `reel.state, reel.stage, reel.error = READY, None, None; db.commit()`
   (`reel_task.py:71`) — with no re-fetch/compare of current state, this **overwrites the FAILED
   row back to READY**, silently reversing what the admin was shown, after a second render may
   already be in flight or done.

Impact: duplicate Bedrock/Transcribe/Fargate spend, confusing state flips visible to admins,
and the single-active-reel invariant is not actually enforced end-to-end.

Fix: before the final write in both the success and failure paths of `reel_task.run`/`_render`,
re-fetch (or use `SELECT ... FOR UPDATE` / a `WHERE state = 'rendering'` guarded `UPDATE`) and
only write the terminal state if `reel.state == RENDERING`; else log and no-op, mirroring
`run_task.py`'s `job_state in (...)` guard.

### 2. TOCTOU race lets two concurrent `POST .../reels` create two active reels for one job

`reel_service.create_reel` (`reel_service.py:73-90`) checks
`any(r.state in ACTIVE_STATES for r in list_reels(db, job))` and only *after* that passes,
inserts + dispatches. There is no DB-level constraint (no unique partial index in
`alembic/versions/0130_webinar_video_reels.py`) and no row lock (`with_for_update`, advisory
lock) serializing this check against a second, near-simultaneous request. Two admins (or one
admin double-clicking before the button disables, or two browser tabs) POSTing within the same
window can both read "no active reel" and both insert + launch an ECS task, each paying for its
own Bedrock/Transcribe/Fargate run.

This was explicitly called out as a thing to check and there is no test for it in
`tests/video_pipeline/test_reels.py` (only sequential `test_one_reel_renders_at_a_time`, which
relies on a *pre-existing* row committed before the request, not on two requests racing each
other).

Fix: add a partial unique index, e.g.
`CREATE UNIQUE INDEX ... ON webinar_video_reels (job_id) WHERE state IN ('pending','rendering')`,
and catch the resulting `IntegrityError` in `create_reel` to raise `ReelConflict` — this also
closes the same-request-twice case that `ACTIVE_STATES` alone cannot guarantee under concurrency.

## Medium

### 3. ECS RunTask refusal leaves a reel `pending` with no retry and no distinguishing message,
blocking new attempts for up to 60 minutes

`task_dispatch.dispatch_reel` (`task_dispatch.py:149-172`): when ECS is unconfigured or
`RunTask` throws, the reel is left `PENDING` (by design, per the docstring — "nothing retries
it"). But `PENDING` is in `ACTIVE_STATES`, so `create_reel` refuses all further attempts for that
job (`ReelConflict("A reel of this recording is already being made.")`) until `_expire_stale`
flips it to `FAILED` an hour later. An admin hitting a real ECS misconfiguration sees a generic
"already being made" 409 with no actionable detail, and is locked out of retrying for up to an
hour. Given this is reachable by ordinary user action (create with ECS transiently unavailable),
worth surfacing the real cause sooner — e.g. distinguish "not configured"/"refused" in the error
shown, or don't count a `PENDING` reel with no `ecs_task_arn` as blocking a fresh attempt.

### 4. Frozen preview URL can silently go stale (403) with no error UI or recovery

`app/components/admin/video-pipeline-reel-item.tsx:29-44` (`useStablePreviewUrl`) intentionally
freezes the presigned URL the moment a reel turns `ready`, by design, to avoid restarting
playback on each poll — but polling stops entirely once nothing is `pending`/`rendering`
(`shouldPollReels`, `video-pipeline-reels-display.ts:53-55`), so the URL is *never* refreshed
again for that mount. `PREVIEW_EXPIRES_IN = 3600` (`reel_service.py:34`). If an admin leaves the
job page open past that hour (easy: it's the same tab used to watch the render, then left open),
the `<video>` element's `src` becomes a dead presigned URL with no error boundary or retry —
playback just fails with a generic browser video error and no indication why. Contrast with the
frames card, which has an explicit `useEffect` refetch timed to the presign expiry
(`$jobId.tsx:171-175`); reels have no equivalent. Low-cost fix: add the same expiry-driven
`revalidate()` pattern, gated so it doesn't restart a *currently playing* video (e.g. only
revalidate, and only swap `src` if the `<video>` is paused / far from expiry).

### 5. Vimeo upload runs synchronously in the request and can leak internal exception text to the
admin error toast

`reel_service.upload_to_vimeo` (`reel_service.py:93-118`) is fine for `vimeo.VimeoError`, but the
generic `except Exception as exc: raise ReelUploadError(f"Could not upload the reel — {exc}")`
(`:111-113`) forwards `str(exc)` verbatim into a 502 body, which the frontend shows as-is via
`toast.error` (`video-pipeline-create-reel-dialog.tsx` / reel item). For a super_admin-only
screen this is a low-severity internal-detail leak (could include S3 client / boto3 internals,
file paths), not PII, but worth trimming/logging-only for anything other than a known
`VimeoError`.

## Low / Informational

- `reel_task.py:102` truncates the raw exception string into `reel.error` and shows it verbatim
  in the admin UI (`video-pipeline-reel-item.tsx:76-79`). Same class of issue as #5 — acceptable
  for a super_admin-only screen, flagging for awareness since it's a change from user-facing
  surfaces elsewhere in the app that scrub errors.
- `trailer_select.build_prompt` (`trailer_select.py:128-160`) inlines the admin's free-text
  `focus` prompt (≤500 chars) directly into the Bedrock user message. Since only a super_admin
  can set it and the only effect is steering which of the admin's own transcript sentences get
  picked, this isn't an exploitable prompt-injection path today — noting only because it's new
  untrusted-ish input reaching an LLM call.
- `zoom_recording_fetch.fetch_recording` now always attempts a camera-rendition download for
  *every* job (`zoom_recording_fetch.py:270-276`), not just when a reel is requested — intentional
  per the archiving design (reels need it after Zoom deletes its own copy at publish), but it does
  add S3 storage + per-job bandwidth/time to every webinar job going forward, worth confirming
  that's an accepted tradeoff rather than an oversight.
- `s3:DeleteObject` in `modules/ecs-task/main.tf:193-200` is correctly scoped to
  `video-pipeline/reels/*/tmp/*`, matching `trailer_words.transcribe_words`'s actual key
  (`{scratch_prefix}/reel-audio-{uuid}.flac` under `reel_task.scratch_prefix`). No issue found
  here — flagging as verified rather than a finding.

## Positive Observations

- `AdminDep` correctly enforces `super_admin` only (`src/auth/deps.py:61`) on both reel endpoints.
- S3 upload sets `ContentType: video/mp4` for the finished reel (`reel_task.py:70`) and
  `video/mp4`/`text/vtt`/`audio/flac` elsewhere — no missing-ContentType issue.
- `_aware()` (`reel_service.py:49-50`) correctly normalizes SQLite's naive `updated_at` vs
  Postgres's tz-aware column for the stale-timeout comparison; test
  `test_the_list_previews_ready_reels_and_fails_abandoned_ones` exercises this against SQLite and
  passes — no tz bug found.
- `blocked_reason`'s S3/Zoom calls are correctly skipped while any reel is actively
  rendering/pending (`reel_router.py:42-48`), so the polling loop itself does not pay that cost
  repeatedly — only an idle page load/poll-tail does, which is cheap and infrequent.
- Camera archive failure is non-fatal to the main job (`archive_original.py:94-99`, tested by
  `test_a_camera_that_will_not_archive_does_not_fail_the_job`).
- Frontend action intents are validated server-side per-intent (`$jobId.tsx:91-134`); orientation
  and prompt length are re-validated on the backend (`VideoReelCreate`, 422 on violation) even
  though the frontend already constrains them — good defense in depth.
- `list_reels`/`_load_job` are single-query, no N+1.

## Unresolved Questions

- Is the ~60 minute `STALE_AFTER` window sized against real observed p99 render time (Bedrock +
  Transcribe + Fargate cold start), or just the local "few minutes" ffmpeg benchmark noted in the
  comment? That affects how often finding #1 is likely to trigger in practice.
- Is a duplicate reel render (finding #1/#2) purely a cost/UX nuisance, or does anything downstream
  (e.g. Vimeo folder listing, analytics) assume at most one `ready` reel per job per orientation?

Status: DONE_WITH_CONCERNS
Summary: No critical/security issues; two High findings on reel-task state races (unguarded
terminal writes vs. the stale-timeout, and a TOCTOU on concurrent create requests) that both stem
from missing the same-state-guard pattern `run_task.py` already uses, plus Medium UX/robustness
gaps around stuck-pending reels and stale preview URLs.
