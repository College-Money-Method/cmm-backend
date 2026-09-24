# Admin trailer reels — plan

Status: in progress · Repos: cmm-backend, cmm-frontend (cmm-infra: one IAM line)

## Outcome
Super admin opens a published job's detail page → "Create reel" modal (landscape | portrait, optional focus prompt e.g. "Focus on merit aid") → ECS task renders the ~60 s reel → preview plays from a presigned S3 URL → "Upload to Vimeo" button.

## Decisions
- **Sources**: the original transcript + S3 archive, not Vimeo.
  - Cues: `transcript.json` (frames prefix) → archived `source.vtt` rebased by the trim offset → Vimeo's track as a last resort.
  - Screen: archived `source.mp4`. Vimeo's copy is trimmed (a different clock), re-encoded, and embed-only.
- **Camera gap**: Zoom's `active_speaker` was never archived and is deleted at publish.
  - From now on, the pipeline archives it as `camera.mp4` next to `source.mp4`. Best effort; a failure never fails the job.
  - A reel reads the archived camera → the Zoom copy if it still exists → otherwise it is blocked with a reason.
- **Execution**: same ECS task image/definition, command `python -m src.video_pipeline.reel_task --reel-id`. The Vimeo upload runs synchronously in the API (the file is ≤ 40 MB).
- **Storage**: `video-pipeline/reels/{job_id}/{reel_id}.mp4`, no expiry. The Transcribe temp audio goes under the same prefix and is deleted. Infra grants `s3:DeleteObject` on `video-pipeline/reels/*`.
- **Vimeo**: `create_video` (embed-only privacy). Folder: `VIMEO_REEL_FOLDER_URI` if set, else the library root.
- Stages also include `starting` (dispatch) and `downloading`.
- Stale `pending`/`rendering` reels (> 60 min without an update) are shown as failed.

## API (prefix /api/v1/admin/video-pipeline, super_admin)
- `GET  /jobs/{job_id}/reels` → `{items: VideoReel[], blocked_reason: str|null}`
- `POST /jobs/{job_id}/reels` `{orientation: "landscape"|"portrait", prompt: str|null ≤500}` → 201 `VideoReel`; 409 when blocked or a reel of this job is already rendering
- `POST /reels/{reel_id}/vimeo` → `VideoReel`; 409 not ready / already uploaded; 502 Vimeo error
- `VideoReel` fields:
  - `id`, `job_id`, `orientation`, `prompt`
  - `state` (pending|rendering|ready|failed), `stage` (selecting|cutting|transcribing|captioning|rendering|uploading|null)
  - `hook_title`, `duration_seconds`
  - `preview_url` (null unless ready), `preview_expires_in`
  - `vimeo_video_id`, `vimeo_url`
  - `error`, `created_at`, `updated_at`

## Phases
1. [x] Backend: model + migration 0130, camera archiving, reel sources/build/task/service/router, focus prompt in `select_segments`, tests
2. [x] Frontend: API client, reels card + create dialog on `admin/video-pipeline/$jobId.tsx`, polling, `<video>` preview, upload button
3. [x] Infra: `s3:DeleteObject` on `video-pipeline/reels/*/tmp/*` only — written, not applied (the user applies)
4. [ ] Verify: pytest ✓ (540), frontend typecheck/lint/tests ✓ (266), code review ✓ — fixed: guarded terminal writes, one-active-reel partial unique index, ECS refusal fails the reel, preview URL re-signed on error; a real `reel_task` run on 22155fa0 needs migration 0130 on prod plus a row insert, so it is waiting on the user's go-ahead

## Non-goals
Recovering Zoom-trashed recordings; editing the reel's segments by hand; public/social posting.
