# Video pipeline — gluing the pieces together

Status: in progress. Six items, two repos, two migrations.

## Decisions taken before writing code

- **No `workshop_id` column on `webinar_video_jobs`.** `webinars.workshop_id` is
  `NOT NULL`, so `job.webinar.workshop` already reaches the workshop; a second
  column could only ever disagree with it. `job_views.to_summary` already
  surfaces `workshop_name`. Audit runs have no webinar and so no workshop — that
  is a property of an audit run, not a missing column.
- **Vimeo has no webhook for transcript readiness.** The main API has no
  transcode or caption event at all (OTT webhooks are commerce events), which is
  why `wait_for_transcode` already polls. So captions cannot be pushed to us.
- **Captions run after publishing, as their own tracked unit of work** — the
  shape the operator asked for. `state` stays `pending→processing→chaptering→
  published` with `published` terminal; caption progress lives in its own
  `captions_state` column so the state machine is untouched.
- **Caption locales: `es`, `zh`, `zh-Hant`.** Not Vietnamese, not the other
  seven in `SUPPORTED_LOCALES`.
- **The ready email goes to `VIDEO_PIPELINE_ALERT_EMAIL` only** — the inbox the
  failure alerts already use. Nothing school-facing.
- **Audit runs do get captions** (the audit video is what the audit reads) but
  no embed code and no ready email, exactly as today.
- **`VideoRunCreate.webinar_id` stays on the API.** The modal stops sending it;
  the field still names an audit video for anything calling the endpoint
  directly. Removing it would delete working behaviour to satisfy a UI change.

## A — Zoom webhook → webinar/workshop → pipeline

Already built: `zoom/webhook_router.py` schedules `intake_recording` off
`recording.completed`; `intake.py` resolves the `Webinar` by `zoom_webinar_id`
and creates the job. Work here is verification, not construction.

## B — Manual run modal (frontend)

- `admin/video-pipeline/index.tsx`: "Create a manual run" button top right,
  opening `ui/dialog`; delete the "Replay jobs, newest first…" line and the
  `webinarOptions` loader fetch.
- `video-pipeline-new-run-form.tsx`: drop `webinarOptions`, the `webinarId`
  state and the `SearchableSelect`; one paste field, closes the dialog on success.

## C — Video title

`Workshop #<sequence> <workshop name> <Month D YYYY> <Cohort> Schools`.
Extract `_video_title` out of `process_recording.py` into `video_title.py` so
`publish_service` can name the ready email with the same string. Every part is
optional at the source, so each is skipped when absent; a job with no webinar
keeps today's fallback. `[Audit]` prefix unchanged.

## D — Captions as a follow-up task

- Migration: `captions_state` (`pending|running|done|skipped|failed`),
  `captions_attempts`, `captions_error`, `captions_completed_at`.
- `caption_task.py`: find the English track on the video; absent → count the
  attempt and leave it for the next sweep; past the attempt ceiling → `skipped`.
  Present → `video_cc_service.run_job(job, None, locales)` reused as-is.
- Fourth sweeper pass, after publishing, batched small.
- Surfaced on the job detail API and screen.

## E — Workshop recording thumbnail

- Migration: `workshops.recording_thumbnail_url` (Text, nullable).
- Schemas + admin PATCH + the workshop admin page's second image upload,
  mirroring `workshop_art_url`.
- `vimeo_upload.set_thumbnail`: POST pictures → PUT bytes → PATCH active.
- Applied once, at upload time, from the workshop's current value — which is
  what makes a later thumbnail change affect only later videos.
- Never fatal: a thumbnail is cosmetic and must not fail a published replay.

## F — Embed code + ready alert

The embed code write already exists. Add `notify.notify_published`, sent after
the embed code and before `published`, skipped for audit runs.

## Verification

`uv run pytest tests/video_pipeline/`, `tests/video_cc/`, `tests/workshops/`;
frontend `tsc --noEmit` and `vitest run`. The analytics collection error is
pre-existing and out of scope.
