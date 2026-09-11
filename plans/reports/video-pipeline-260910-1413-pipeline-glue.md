# Video pipeline — gluing the pieces together

Date: 2026-09-10 · Repos: `cmm-backend`, `cmm-frontend`

Six items, in the order they were asked.

## A. Zoom webhook → pipeline (already built, verified not rebuilt)

Nothing to add. The chain already exists end to end:

`src/zoom/webhook_router.py` `recording.completed` → `_schedule_recording_intake`
(reads `object.uuid` + `object.id`, ignores the `download_token` deliberately) →
background `intake.intake_recording(zoom_webinar_id, recording_uuid)` →
`select(Webinar).where(Webinar.zoom_webinar_id == …)` →
`job_service.create_from_recording(db, webinar_id=…, zoom_recording_uuid=…)` →
`task_dispatch.dispatch`.

The job therefore already holds the webinar, and `webinars.workshop_id` is NOT
NULL, so the workshop is reachable from the job by join. No `workshop_id` column
was added — a second path to the same row is a second thing that can disagree.

Operational precondition: the Zoom app must have `recording.completed`
subscribed. Nothing in this change can verify that from here.

## B. Manual run moved into a modal (frontend)

- `app/components/admin/video-pipeline-new-run-form.tsx` — same component,
  now a `Dialog`. Props are `open`/`onOpenChange`; closes and clears the source
  field on success, stays open on an API refusal (the paste is what needs
  fixing). Webinar picker removed entirely: naming a video after a webinar is
  what the automatic path does, and an audit run must not imply it might publish
  there. `VideoRunCreate.webinar_id` stays on the API; the modal just stops
  sending it.
- `app/routes/admin/video-pipeline/index.tsx` — "Create a manual run" button top
  right; the `Replay jobs, newest first. N total · showing x–y` line is gone;
  the loader no longer fetches past webinars (one fewer request per page view).
  `to` is kept because the pager's "Next" disables on it.

## C. Video titles

`src/video_pipeline/video_title.py` builds
`Workshop #1 Navigating the New System of College Pricing and Financial Aid November 17 2025 MOUNT Schools`
— workshop name (not webinar name), workshop date, cohort. Dated in
`settings.workshop_display_timezone`, because a 7pm Eastern session is stored as
the next day in UTC and a title a day off is a title that looks wrong to
everyone who attended. Missing pieces are omitted rather than filled with
placeholders; audit runs keep the `[Audit] ` prefix.

Used in three places now: the Vimeo video name, the iframe `title` in the embed
code, and the ready email's subject — one source, so a screen reader and a
school's page cannot disagree about what the video is called.

## D. Video CC folded into the pipeline

Vimeo has **no webhook** for transcode or transcript readiness (verified against
the API last session). So: publish first, translate after — as agreed.

- Locales: `settings.video_caption_locales = "es,zh,zh-Hant"`. Vietnamese
  excluded per instruction.
- `src/video_pipeline/caption_task.py` — `due()` selects `published` jobs with a
  Vimeo id whose `captions_state` is `pending`, or `running` and untouched for
  60 minutes (a crashed process leaves `running` behind; re-running is safe
  because each language's track is replaced wholesale). `run()` skips when there
  are no locales, counts an attempt and stays `pending` while Vimeo has no
  English track, and gives up as `skipped` at `video_caption_max_attempts` (48
  sweeps ≈ 4h). A Vimeo listing error returns without spending an attempt.
- Sweeper gained a fourth job, `caption_published_jobs`, running last and one
  job per pass — a replay being live matters more than its Spanish subtitles.
- The registry in `video_cc_jobs.py` documents itself as event-loop-owned and
  the sweeper is an APScheduler thread, so the sweeper **constructs** a
  `VideoCcJob` instead of calling `create_job`. No cross-thread
  `asyncio.Event.set()`, no registry mutation.
- Progress lives in four new columns (`captions_state`, `captions_attempts`,
  `captions_error`, `captions_completed_at`), not in `state`: `PUBLISHED` is
  `frozenset()` — terminal on purpose, because it is the state that means a
  school's page has a working player.
- Migration `0122` backfills every existing row to `skipped`. Adding a column
  should not send the next sweep off to translate the entire back catalogue.

## E. Workshop recording thumbnail

- `workshops.recording_thumbnail_url` (migration `0121`), in
  `WorkshopCreate`/`Update`/`Out` and all five `WorkshopOut` builders. The
  school-facing `WorkshopSummary`/`WorkshopPortalItem` are untouched — this is an
  admin field.
- `src/video_pipeline/thumbnail.py` — read at upload time, never re-applied.
  That is what makes replacing the image affect only sessions recorded
  afterwards. Vimeo's three-step protocol (`POST /pictures` → `PUT` bytes to the
  pre-signed link with no auth header → `PATCH active:true`). Never fatal: by
  the time it runs the video is uploaded, and failing the run over a poster frame
  would throw away an hour of processing. Guards on content-type, 10MB, and
  private hosts.
- No image configured ⇒ `setting_thumbnail` is dropped from `expected_stages`,
  so the timeline shows work that was never part of the job rather than a step
  stuck pending.
- Frontend: second `useImageUpload` block on the workshop detail form, with copy
  saying the change applies to later sessions only; `setting_thumbnail` label
  added to the stage map.

## F. Embed code + ready alert

`publish_service._write_embed_code` now titles the iframe with `video_title(job)`.
`notify.notify_published` sends `[Video pipeline] Ready — {title}` to
`settings.video_pipeline_alert_email` (ops only, nothing school-facing), with
the Vimeo watch link and chapter count. Sent **after** `advance(…, PUBLISHED)`,
so the alert cannot claim a replay is live for a transaction that then fails,
and it can never raise. Audit runs are silent — they publish nothing anyone can
see. The body says captions follow separately, because at send time they have
not started.

## Verification

- `uv run pytest tests/video_pipeline/ tests/video_cc/ tests/workshops/` →
  **543 passed**. New: `test_caption_task.py` (13), `test_video_title.py` (5),
  `test_thumbnail.py` (8), plus additions to `test_vimeo_upload.py`,
  `test_notify.py`, `test_sweeper.py`, `test_stage_progress.py`,
  `test_job_stream.py`, `test_audit_run_processing.py`. No test weakened or
  skipped; two `stage_plan` tests were updated because `PLAN` legitimately grew
  a step, and the no-image case got a test of its own.
- The analytics test-collection failure is pre-existing and untouched, as are
  two failures in `tests/analytics/test_analytics_endpoints.py` (verified by
  re-running that file with the new root conftest removed).
- Full run: `uv run pytest tests/ --ignore=tests/analytics/test_resource_breakdown_queries.py`
  → **1097 passed**, 2 pre-existing analytics failures.

### The suite was mailing a real inbox (found and fixed after the fact)

Adding the ready alert to the end of `publish_service.publish()` gave `publish()`
a side effect that leaves the machine, and `test_publish_service.py` — the one
file that runs `publish()` end to end — patched nothing. The suite loads `.env`,
so it has live AWS credentials, and `video_pipeline_alert_email` is on
`SANDBOX_DOMAIN`, which `send_email` lets through even in sandbox mode. Roughly
ten real alerts went to the ops address describing a fixture webinar and the
made-up Vimeo id `987654321`. Nothing school-facing: that address is the only
recipient.

Two changes close it:

- **`tests/conftest.py` (new)** — an autouse fixture swaps
  `src.emails.ses_client._create_ses_client` for an offline stub, so no test in
  any package can reach SES by omission. The send still runs and still writes
  its `email_send_log` row; it stops at the network edge. A stub that *returns*
  rather than raises was chosen deliberately: `notify_published` catches
  `Exception` broadly, so a raising stub would be swallowed and hide the very
  problem it was meant to surface. Per-test patches in `tests/emails/` still
  win for their duration.
- **`test_publish_service.py`** — an autouse `alerts` fixture captures the send
  locally, plus two tests: the alert goes out on a successful publish, and a
  publish that fails at Vimeo announces nothing.
- Frontend: `tsc --noEmit` clean; `vitest run` → **218 passed, 0 failed**. The 42
  failing *suites* are all pre-existing collection errors in `.claude/scripts`
  (40) and Playwright specs under `tests/mobile-qa` (2) — none in `app/`.

Status: DONE_WITH_CONCERNS
Summary: All six items implemented across both repos. Item A needed no code —
the webhook path already existed and was verified rather than rebuilt. Captions
run as a tracked follow-up after publishing, since Vimeo has no readiness
webhook. Backend 543 tests pass; frontend typechecks with 218 tests passing.
Concerns/Blockers:
- **Migrations 0121 and 0122 have not been run against any database.** The
  running dev API (`--env-file .env.local`, port 8001) will 500 on the new
  columns until `uv run alembic upgrade head` is applied. Not run here because
  it changes a shared database — say the word and it takes one command.
- Nothing is committed in either repo.
- The Zoom app must have `recording.completed` subscribed for item A to fire in
  production; unverifiable from this side.
- The ready email is sent at publish time, so it goes out before translated
  captions exist. Deliberate, per the "publish first" decision, but it means
  "ready" and "fully captioned" are not the same moment.
- `scratchpad/e2e.log` and `e2e2.log` hold raw Vimeo tus upload tokens in logged
  PATCH URLs. Move them somewhere durable-and-private or delete them; never
  paste them anywhere public.

Unresolved questions:
1. `video_caption_max_attempts = 48` (≈4h of sweeps) is a guess at how long
   Vimeo can take to write an English transcript for a 70-minute session. Real
   runs will show whether that is generous or tight.
2. A `captions_state = failed` job has no retry control on the admin screen —
   only the sweeper's 60-minute stale-`running` reclaim. Worth a button?
3. Should `skipped` captions be visible as a warning anywhere, or is the job
   detail screen enough? Right now it is styled as an ordinary outcome.
