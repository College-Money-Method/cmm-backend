# Research: 1-minute trailer reel from webinar recordings

Date: 2026-09-23 | Scope: research only, no code changed

## TL;DR

- Use Bedrock to **pick** the moments, not to **generate** the video. Claude reads the transcript and chapters we already have, returns 3–6 segments (~60s total), and ffmpeg cuts the **real footage** into a 9:16 reel with captions.
- Generative video (Nova Reel / Luma Ray on Bedrock) makes new synthetic footage from a prompt. It cannot reuse the speaker, the slides or the voice, so it does not produce a trailer of *this* webinar. At most it could make a branded intro card, and a static template does that more cheaply.
- Almost everything needed already exists: the archived source in S3, the trim offset, the transcript re-based to the trimmed clock (`transcript.json`), chapters, frame classifications, the Bedrock client with usage tracking, the ECS ffmpeg task, and the "side-channel after publish" pattern used by captions.
- Estimated cost is under $0.10 per reel. Rendering takes a few minutes on the existing Fargate task size.

## Current pipeline (verified)

| Piece | Where | Relevance |
|---|---|---|
| Webhook → idempotent job → ECS task | `src/zoom/webhook_router.py:158`, `intake.py:28`, `task_dispatch.py:90` | New task mode rides the same dispatch |
| Trim + frame sampling (ffmpeg, Fargate 2 vCPU/4 GB/40 GB) | `process_recording.py:198`, `ffmpeg_ops.py`, `Dockerfile.video` | Only image with ffmpeg; API `Dockerfile` has none, so the render **must** run as an ECS task |
| Untrimmed source + raw VTT archived (Glacier IR) | `archive_original.py:74,105` (`restore_original`) | Reel can be rendered days/weeks later: restore + re-apply `trim_offset_seconds` |
| Transcript = Zoom VTT, **cue-level** (few-second phrases, no word times) | `transcript.py:18,35`; re-based cues in `frames_prefix/transcript.json` | Good enough for cut points at cue boundaries + cue-level captions |
| Chapters (JSONB on job) + frame types (`title_card/content_slide/speaker/screen_share_other/blank`) | `models.py`, `frame_classify.py:44`, `chapter_build.py` | Feed to the LLM for narrative structure and to avoid blank/gallery stretches |
| Bedrock via `AnthropicBedrock`, `call_json()`, cost ledger | `bedrock_client.py:69`, `bedrock_usage.py` | Reuse as-is. Only Haiku 4.5 is configured (`config.py:68`), and IAM allows only that inference profile |
| Captions = post-publish side-channel with own columns, not a job state | `caption_task.py:1-20` | **The pattern to copy**: `published` stays terminal, so a failed reel never un-publishes a replay |

## Recommended architecture

```
job reaches PUBLISHED
   │ sweeper picks jobs with trailer_state = pending
   ▼
[API process] select segments  ── Bedrock call_json (transcript cues + chapters + frame types)
   │ validate + snap → trailer_segments JSONB, trailer_state = rendering
   ▼
[ECS task --mode trailer] restore source → re-apply trim → one ffmpeg pass:
   cut N segments → concat → 9:16 layout → burn captions → loudnorm → H.264
   │ upload s3://…/video-pipeline/{job_id}/trailer/reel-9x16.mp4 (+ 16x9 optional)
   ▼
trailer_state = ready_for_review → admin previews, approves / regenerates / nudges segments
```

### 1. Segment selection (Bedrock)

- Input: the numbered re-based cues (`[i] mm:ss text`), chapter titles with timecodes, the frame type per time range, and the webinar title.
- Have the model return **cue index ranges, not free timestamps**. The server converts ranges to times, so cuts always land on cue boundaries, the reel text matches the transcript word for word, and made-up timestamps are impossible.
- Server-side guards, with one retry when the model's output breaks them:
  - 3–6 segments, each 6–20s;
  - total 50–62s;
  - no overlaps;
  - skip any range whose frames are `blank`, or `screen_share_other` for gallery view.
  - Pad each cut by ~0.15s and clamp it to the neighbouring cues.
- Order: the strongest hook first, then chronological. Ask for a one-line `hook_title` to use on the intro card.
- Also ask for `compliance_flags` per segment (specific $ amounts, guarantees/"you will get X aid", school or person names) to show in review.
- Model choice: start with Haiku 4.5, since the client and IAM are already set up. Compare against a Sonnet-class model on 5 real webinars. If Sonnet wins, add a `BEDROCK_SONNET_MODEL_ID` setting and its inference profile to the IAM policy in cmm-infra. Confirm the profile ID in the Bedrock console; don't hard-code it from blog posts.
- A 60–90 min transcript is roughly 15–25k input tokens, so each call costs a few cents at most.

Output shape (keep it small; the server derives everything else):

```json
{
  "hook_title": "The FAFSA mistake that costs families thousands",
  "segments": [
    {"first_cue": 212, "last_cue": 216, "why": "surprising stat", "compliance_flags": ["dollar_amount"]},
    {"first_cue": 40,  "last_cue": 43,  "why": "sets up the problem", "compliance_flags": []}
  ]
}
```

### 2. Rendering (ffmpeg, one pass)

Use one input per segment with **input-side `-ss`**, which gives fast and accurate seeking because we re-encode anyway. This avoids decoding the full 90 minutes. Stream-copy cuts (`-c copy`) snap to keyframes, so they would be seconds off and must not be used here.

```bash
ffmpeg -y \
  -ss 312.40 -t 11.20 -i trimmed.mp4 \
  -ss 1840.0 -t 14.60 -i trimmed.mp4 \
  -ss 2710.3 -t 9.80  -i trimmed.mp4 \
  -filter_complex "
    [0:a]afade=t=in:d=0.05,afade=t=out:st=11.15:d=0.05[a0];
    [1:a]afade=t=in:d=0.05,afade=t=out:st=14.55:d=0.05[a1];
    [2:a]afade=t=in:d=0.05,afade=t=out:st=9.75:d=0.05[a2];
    [0:v][a0][1:v][a1][2:v][a2]concat=n=3:v=1:a=1[cv][ca];
    [cv]split[bg][fg];
    [bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:2[bgb];
    [fg]scale=1080:-2[fgs];
    [bgb][fgs]overlay=(W-w)/2:(H-h)/2,subtitles=reel.ass[v];
    [ca]loudnorm=I=-14:TP=-1.5:LRA=11[a]" \
  -map "[v]" -map "[a]" -r 30 -c:v libx264 -preset medium -crf 20 -pix_fmt yuv420p \
  -c:a aac -b:a 160k -movflags +faststart reel-9x16.mp4
```

- **Layout (v1): blurred-background "fit".** Webinars are mostly slides. A center crop of a 16:9 slide cuts off the content, and face tracking is useless on slides. Fitting the full frame to 1080 px wide (about 608 px tall) over a blurred fill is the standard for slide content and needs no extra services. Captions go in the lower third and the hook title in the top band.
- v2 layout: slide on top with the speaker below, using the Zoom active-speaker thumbnail region (the pipeline already knows its crop fractions, `config.py:238`). Only build this if marketing asks for it.
- **Captions (v1): cue-level.** Re-base the chosen cues onto the reel clock and write a styled `.ass` file for the `subtitles` filter. Debian's ffmpeg includes libass, but check `ffmpeg -filters | grep subtitles` in `Dockerfile.video`.
- **Captions (v2, animated word-by-word):** run AWS Transcribe on **only the ~60s of reel audio**, not the whole webinar. That costs a few cents and gives word timings for karaoke-style `.ass`. WhisperX would make the image much heavier, so skip it.
- **Intro/outro:** pre-rendered 1080×1920 brand clips (logo + `drawtext` hook title; end card "Watch the full webinar" + URL) added as extra `concat` inputs. Any music needs a licensed track; YAGNI for v1.
- Output: 1080×1920, 30 fps, H.264 + AAC, `+faststart`, ~60s. This is accepted by IG Reels, TikTok, YouTube Shorts and LinkedIn. Loudness of -14 LUFS / -1.5 dBTP is a safe default; the platforms don't publish official targets.

### 3. Data model / ops

- Add columns to `WebinarVideoJob`, mirroring captions (Alembic `0130`):
  - `trailer_state` (pending/selecting/rendering/ready_for_review/approved/failed/skipped);
  - `trailer_segments` JSONB;
  - `trailer_key`;
  - `trailer_error`;
  - `trailer_updated_at`.
- Sweeper: select → dispatch the ECS task (count it against `video_pipeline_max_concurrent`) → reclaim stale `rendering` rows.
- Task entrypoint: add a `--mode trailer` to `run_task.py` (same image, same IAM; S3 `video-pipeline/*` is already allowed).
- Admin UI (frontend): a trailer panel on the job detail page with a player (presigned S3 URL), the segment list with transcript text and compliance flags, Approve / Regenerate / edit ranges then re-render, and Download.
- Backfill: past jobs still have `archive_key` + `frames_prefix`, so reels can be generated for older webinars within the archive retention window.

## Cost / time per reel (approximate; check current AWS pricing)

| Step | Cost | Time |
|---|---|---|
| Bedrock selection (1 call, ~20k in / ~0.5k out) | ~$0.02–0.10 depending on model | 5–20s |
| Restore source from Glacier IR (~1–2 GB) | ~cents (retrieval fee) | <1 min |
| ffmpeg render on existing 2 vCPU task | ~$0.01 | ~2–4 min |
| Transcribe 60s (only if word captions, v2) | ~$0.03 | ~1 min |

## Why not the alternatives

- **Generative video (Nova Reel / Luma Ray):** cannot show the real presenter or slides; 6s–2min clips; high latency; the likeness and claims are not what was said. Rejected.
- **Bedrock Data Automation for video:** duplicates chapters and frame classification we already build. YAGNI.
- **MediaConvert:** it can clip, stitch and burn captions, but not a blurred 9:16 composite. ffmpeg is already in the image, so it adds nothing.
- **Rekognition face tracking:** only useful for talking-head crops; our content is slide-heavy. Revisit if marketing chooses a speaker-crop layout.
- **Third-party clip APIs (Opus Clip, Vizard, Shotstack, Creatomate, Descript):** they work, but add per-minute cost, send recordings to a vendor and give us less control over compliance. Useful only as a quick benchmark: run 2–3 webinars through Opus Clip by hand and compare against our output.

## Risks

- **Out-of-context claims:** financial-aid advice cut down to 10s can mislead. Mitigate with compliance flags, **human approval before any reel is released**, and a prompt rule against segments that depend on earlier context ("as I said…", "this one").
- **Cue boundaries mid-sentence:** Zoom cues sometimes split sentences. Mitigate by preferring ranges that end on terminal punctuation; the admin can nudge ranges.
- **PII:** gallery view and attendee names in chat/Q&A. Mitigate by excluding `screen_share_other`/gallery frames and Q&A with attendee names.
- **Presenter consent** for social use of their likeness: a business/legal question, not a technical one.

## Corrections to the external research (don't reuse these)

- The Bedrock model ID `anthropic.claude-sonnet-5-20241022-v1:0` is invented. Use the inference-profile ID from the console.
- AWS Transcribe for 60 min costs about $1.44 at standard batch rates, not $6. Regardless, transcribe only the reel audio.
- `-f concat` + `xfade` and `-c copy` cuts do not work for accurate multi-clip cuts. Use the per-input `-ss` + `concat` filter shown above.
- Its "Nova Reel deprecated Sept 30 2026, Luma Ray v2 successor" claim is unverified. It doesn't matter, since generative video is rejected either way.

## Unresolved questions

1. Where do reels go? A download for marketing to post by hand (v1 recommended), or auto-post through IG/TikTok/LinkedIn/YouTube APIs (much more scope: OAuth, app review)?
2. Is 9:16 only, or also 16:9 or 1:1 (LinkedIn/web)?
3. Who approves? Is there a compliance sign-off, or is the webinar owner enough?
4. Brand assets: intro/outro template, fonts, caption style, music (licensed?).
5. Do all webinars get a reel automatically, or only opt-in per webinar? Should past webinars be backfilled?
6. Do presenters or partner schools need to consent to social use?
7. Is cue-level captioning acceptable for v1, or is word-by-word animated captioning required at launch?
