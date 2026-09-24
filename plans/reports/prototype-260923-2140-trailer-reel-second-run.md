# Trailer reel prototype: second run (Sonnet, presenter-only)

Date: 2026-09-23 | Job `514db214-a2ec-4951-b6ce-8fe3707a0f88` | Follows `prototype-260923-2034-trailer-reel-first-run.md`

## Result

`scripts/output/trailer/514db214/reel-9x16.mp4` (gitignored). The first run is kept in `run1-haiku/` for comparison.

| Property | Value |
|---|---|
| Format | 1080×1920, 30 fps, H.264 + AAC 48 kHz, 64.0 s, 48 MB |
| Loudness | −14.7 LUFS integrated, −1.4 dBFS true peak |
| Boundaries | 3/3 segments start and end on their sentence's first/last word (checked against word timings) |
| Speakers | Paul Martin only; the one attendee (Anjelica Johnson, 212 s) is excluded |
| Selection cost | Sonnet 4.6: 26.6k in / 344 out ≈ $0.085, one attempt, no retry |

Hook: "Are You Making This College Aid Mistake"

1. (26 s) Public universities do not give need-based grants to out-of-state students.
2. (24 s) Admissions uses merit scholarships to meet enrollment goals: ask whether a school offers them, and how generous they are.
3. (14 s) "…the information you need to get started and use all of the great resources on the School Resource Center." This works as a natural CTA.

Compared with run 1 (Haiku): no slide references ("this fundamental equation" is gone), each segment is a complete argument, and the answer was valid first time.

## Changes since run 1

cmm-backend (uncommitted):
- `config.py`: added `bedrock_sonnet_model_id` (default `us.anthropic.claude-sonnet-4-6`), Sonnet $3/$15 rates, and `trailer_presenter_name` (default "Paul Martin").
- `bedrock_client.call_json(model_id=None)`: an optional model override. Every other caller stays on Haiku.
- `bedrock_usage.cost_usd(..., model_id=None)`: prices at Sonnet rates when given the Sonnet id. The ledger passes the model through.
- `trailer_sentences.speakers()`: carries each speaker label forward.
- `trailer_select`:
  - Uses Sonnet.
  - `presenter_mask()` matches the Zoom label by name prefix, case-insensitive. A transcript with no labels counts as all presenter.
  - The prompt marks presenter lines, and non-presenter lines stay in as context.
  - `validate` drops any segment that includes another speaker.
- Script:
  - The cut-snap window is widened from 1.2 s to 2.0 s. Run 1's settings clipped "grants from the university": inside a 19 s Zoom cue the interpolated end was 1.4 s early.
  - Added `_check_boundaries`, which warns when the heard first/last word ≠ the sentence's. Words are assigned by end time, since recognisers stretch the first word after a pause back across the join.
- Tests: 3 added (other-speaker drop, presenter mask, Sonnet cost). 510 pass.

cmm-infra (uncommitted, **applied to dev, prod and shared**), `modules/ecs-task/main.tf`:
- `bedrock_model_id` becomes the list `bedrock_model_ids` (Haiku 4.5 + Sonnet 4.6). No environment set the old variable.
- New `task_transcribe` policy: Start/Get/DeleteTranscriptionJob on `transcription-job/*`. S3 read comes from the existing `video-pipeline/*` grant.
- Applied to dev and prod: 1 added, 1 changed each.

cmm-infra `shared/main.tf` (uncommitted, applied, 1 added): new inline policy `transcribe-trailer-words` on user `cmm.cloud`, with the same three actions. boto3 authenticates as `cmm.cloud` whenever its static keys are in the environment, which is also the reason the RunTask policy already lives on this user. So the task-role grant alone was not enough.

## Findings

- **Sonnet 5 is listed but not enabled.** `us.anthropic.claude-sonnet-5` is ACTIVE in list-inference-profiles, but invoking it returns 403 "not available for this account" (needs a model-access request or AWS Sales). Sonnet 4.6 and 4.5 both work, so 4.6 is the default. Switch via `BEDROCK_SONNET_MODEL_ID` once 5 is enabled, and add it to `bedrock_model_ids`.
- **Transcribe: verified on job 22155fa0 (below).** For job 514db214 the word timings came from local faster-whisper.
- **Camera fallback.** The script falls back to `speaker_view` when `active_speaker` is absent. It does not fall back to the shared-screen file, which would show slides.

## Third run: latest prod job, real Transcribe

Job `22155fa0-b86d-49ff-b554-1ea03fce599a` (recorded Sep 23, 88 min, trim 8.45 s). Output: `scripts/output/trailer/22155fa0/reel-9x16.mp4`.

| Property | Value |
|---|---|
| Format | 1080×1920, 30 fps, H.264 + AAC 48 kHz, 53.7 s, 39 MB |
| Loudness | −14.5 LUFS integrated, −1.4 dBFS true peak |
| Words | AWS Transcribe, 163 words |
| Boundaries | 3/3 ✓ |
| Selection | Sonnet 4.6, about $0.10, one attempt; 484 cues → 807 sentences |

Hook: "How Families Pay Less for College Senior Year"

1. (16 s) The most generous merit aid comes from the colleges themselves.
2. (23 s) Merit decisions are made by admissions, not by financial aid.
3. (15 s) Admissions uses merit scholarships to meet its enrollment goals.

Getting the source file:
- The recording was already in Zoom's trash. It was recovered with `PUT …/recordings/status {"action":"recover"}`, active_speaker was downloaded (1.2 GB), and the recording was re-trashed afterwards. It is back to 404.
- The temporary S3 audio is deleted: the `…/trailer/` prefix is empty.
- No Transcribe jobs remain.

Notes:
- 53.7 s is inside the 45–65 s range but at the short end. Segments 2 and 3 overlap in theme (both say "merit = admissions"); a reviewer might swap one.
- The script now mutes httpx INFO logs, which were printing the presigned Transcribe result URL.

## Next

1. Production integration, as in the run-1 report: archive `active_speaker` before trashing, migration, `--mode trailer` task + sweeper, admin review UI, Vimeo upload on approval.

## Unresolved questions

1. Sonnet pricing ($3/$15) is assumed from list price. Verify it against the AWS bill once the ledger records real rows.
2. The hook title is a question without "?"; Sonnet dropped the punctuation. Fine as-is, or should the prompt ask for punctuation?
3. Commit the cmm-infra changes (applied but uncommitted), and the cmm-backend changes?

## Branding pass and landscape option (job 22155fa0)

| File | Size | Loudness |
|---|---|---|
| `reel-16x9.mp4` (new default) | 1280×720, native, no upscale, 53.7 s | ≈ −14.5 LUFS |
| `reel-9x16.mp4` | 1080×1920, crop, 2.7× upscale | ≈ −14.5 LUFS |

The old yellow/Inter render is kept in `v1-yellow-inter/`.

Changes:
- **Font.** Captions and cards now use Lora Bold, the site's heading face. `fonts/` now holds only `Lora-Bold.ttf` + OFL; Inter is removed.
- **Highlight.** The spoken word sits on a cmm-teal block, a thick teal outline, where it used to turn yellow. The other words are white with a brand-950 edge.
- **Lower-third strip.** A teal strip with "Paul Martin · College Money Method" and a sea-glass progress bar. It also hides the name tag and clock that Zoom burns into the corners; the clock jumped at every cut.
- **Presenter label.** Taken from Zoom's speaker label for `trailer_presenter_name`.
- **Orientation.**
  - `--orientation landscape|portrait`, default landscape.
  - `Layout` per orientation in `trailer_captions.py`. Landscape allows ≤5 words and ≤30 chars per group; portrait ≤3 and ≤18.
  - `render_vertical` becomes `render_reel(orientation=…)`.
  - Outputs are named `reel-{16x9|9x16}.{ass,mp4}`, and stages 1–4 are shared by both.
- **Why portrait is soft.** The active_speaker source is 1280×720, so the 9:16 crop is 405 px wide. That is a limit of the source, not something the encode can fix.
- **Tests.** 12 trailer tests pass, one of them new (landscape groups).

## Caption style C (job 22155fa0)

The Lora captions were hard to read over the gingham shirt. Six mockups are in `caption-options/`; the user picked C.

- **Captions:** white Inter ExtraBold on an opaque cmm-teal bar (BorderStyle 3, padding = size/5). The bar was 90% opacity at first, which showed moving seams: libass draws one box per styled run, so where the spoken word's box overlapped its neighbours the bar got darker. An opaque bar hides the overlap. The spoken word turns cmm-flax. The pop animation is dropped because it would bulge the bar.
- **Size:** caption size ×0.85 (portrait 85, landscape 68), so a group's bar fits inside the side margins.
- **Unchanged:** title card, CTA and strip stay in Lora. That matches the site, which sets body text in Inter and headings in Lora.
- **Fonts:** `fonts/` holds `Inter-ExtraBold.ttf` (rsms Inter 4.1) and `Lora-Bold.ttf`. The licences are now `Inter-OFL.txt` and `Lora-OFL.txt`.
- **Renders:** re-rendered `reel-16x9.mp4` (10.7 MB) and `reel-9x16.mp4` (32.9 MB), both 53.7 s. The previous render is in `v2-lora-teal-block/`.
- **Tests:** 12 trailer tests pass. The ASS test now checks the Caption style is Inter and the Title style is Lora.

## Camera ↔ shared-screen mix (job 22155fa0)

- **Source:** the job's archived original, `video-pipeline/originals/<job>/source.mp4` (Glacier IR, 927 MB, ≈ $0.03 per GET). It is Zoom's `shared_screen_with_speaker_view` at 1920×1080. Its duration is identical to active_speaker (5316.33 s), so both files are on one clock. Shots are read from it by the same clock (see next section). The audio is always the camera clip's.
- **Rhythm** (`trailer_edit.screen_windows`; the module was `trailer_screen_mix`):
  - Segment 1 stays on camera (hook and title card).
  - Every later segment goes to the screen 2.0 s after its start and returns 2.5 s before its end, so the jump cuts between segments land on the camera.
  - A visit shorter than 4 s is skipped.
  - The last 3.5 s (the end card) stays on camera.
  - This job: screen at 18.1–36.5 s and 41.0–50.1 s.
- **Transition:** superseded, see next section.
- **Portrait:** the slide is 1080×608 at y=160 on brand-950, with the presenter cropped below (1.6× upscale, against 2.7× for the full crop).
- **Slides matched the speech:**
  - Segment 2 plays over the need-based vs merit-based table.
  - Segment 3 plays over "Admissions uses merit scholarships to meet enrollment goals".
  - Zoom's clock in the bottom-right sits under the strip.
- **Script:** `--screen/--no-screen` (default on). `inputs.json` now carries `archive_key`. For this job's cached inputs it was set by hand to the listed S3 prefix.
- **Outputs:** `reel-16x9.mp4` and `reel-9x16.mp4`, 53.7 s. The camera-only renders are in `v3-camera-only/`.
- **Tests:** 14 trailer tests pass. Two are new: the window rule, and the xfade offsets and trims.

Known limits:
- Captions cover the bottom of a slide. This job's segment 2 hides "Work-Study / Loans".
- Portrait slide text is small on a phone.
- The Zoom thumbnail on the slide duplicates the face in portrait.
- The screen is shown whatever it holds (a mis-shared window, or an agenda slide). Admin review is the safeguard; a later step could let Sonnet pick windows from slide text.
- Production needs no new archive for the screen: the pipeline already archives this file. active_speaker still needs archiving before Zoom trashes it.

## Transitions: dissolve between segments, shrink into the thumbnail (job 22155fa0)

Replaces the shrink-card-at-every-join and `slideleft` attempts, which were not what was wanted.

- **Segment → segment** (e.g. "how can we be successful?" → "Whereas merit-based…"): 0.5 s dissolve (`xfade=fade`).
- **Camera → screen:** over 0.8 s the full-frame camera shrinks (smoothstep) until it sits exactly on Zoom's own thumbnail, then the screen shot carries on. **Screen → camera** is the reverse: the thumbnail grows to full frame.
  - Landscape target: `ZOOM_THUMBNAIL` = top-right sixth (320×180 at 1600,0 on 1920×1080), measured at 40 dB PSNR against the camera file.
  - Portrait target: the presenter's 9:16 crop inside the wider crop under the slide (x=216, y=768, 648×1152 at crop_x 0.5), so it lands seamlessly there.
- **Handles:** a dissolve needs footage past each cut, which the joined clip lacks, so every shot is read straight from the untrimmed recordings (`-ss/-t`, half a transition either side + one frame of slack). Each join is `xfade` at `edge − half`: the picture keeps the joined cut's timeline exactly; the audio is still `reel-cut.mov`'s. Checked: camera frames match the cut at 45 dB at 0 offset, below at ±1 frame.
- **Gotcha:** xfade's custom expression runs on several threads and `st()`/`ld()` are shared between them → noise. Written out without variables. Inputs must be yuv444p.
- **Code:** `trailer_edit.py`: `Shot`, `plan_shots`, `read_span`, `join_graph`, `shrink_between`. `render_reel(camera=, shots=, screen=)` builds one input per shot. `reel-screen.mov` and `--transition` are gone.
- **Renders:** `reel-16x9.mp4` 53.7 s, 1.25 Mbps; `reel-9x16.mp4` 53.7 s, 4.3 Mbps. Stage 6 takes about 50 s for landscape.
- **Tests:** 17 trailer tests pass (shot plan, read spans, join graph kinds and offsets, single-shot pass-through).
- **Known:** in the last frames of a shrink (first of an expand) Zoom's burned-in name tag and clock show in the camera's bottom corners, since the strip only covers them at full frame.

## Portrait caption band (job 22155fa0)

On portrait screen shots the presenter sits under the slide, right where the captions were (560 px up), so they covered the face.

- A 120 px cmm-teal band (`drawbox`) now sits between slide (160–768) and presenter (888–1920; 1.43× upscale, was 1.6×). It is as tall as the caption bar (119 px rendered), so during screen shots the captions, centred on it (`Layout.band_centre`, `\an5\pos`), read as text on the band. Camera shots keep them on the shirt.
- A caption showing across a camera↔screen switch is split at the switch, so it moves with the picture.
- Stage 5 now takes the screen windows (`build_ass(screen=…)`), so portrait re-runs need `--from 5`. Landscape unchanged.
- 18 trailer tests pass.

## Bitrate and resolution

- Captions and strip do not inherit the source bitrate: libass draws them at output resolution, then x264 encodes the whole frame at crf 20. Their sharpness depends on output size and crf only.
- Sources: camera 1280×720 at 1.76 Mbps; shared screen 1920×1080. Landscape is rendered at 1280×720, so slides are downscaled.
- Options: crf 18 (≈ +30% size), and/or a 1920×1080 landscape (slides native, camera upscaled 1.5×).
- Zoom: 1080p camera recording needs "Group HD" enabled by Zoom Support, plus a 1080p webcam, enough bandwidth, and a viewer on full-screen active speaker. The shared-screen file is already 1080p.
  - https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0066166
  - https://community.zoom.com/t5/Zoom-Meetings/Cloud-Recording-Resolution-in-Active-Speaker-View/m-p/150439
  - https://jonnyelwyn.co.uk/film-and-video-editing/how-to-record-a-zoom-meeting-in-high-quality-for-video-editing/

Unresolved: crf 18 and/or 1080p landscape? Mask Zoom's name tag/clock during the shrink, and the shared screen's clock above the portrait band?
