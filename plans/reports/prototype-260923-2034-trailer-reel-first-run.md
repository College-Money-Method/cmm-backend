# Trailer reel prototype: first run on a prod job

Date: 2026-09-23 | Job `514db214` ("Applying for Financial Aid and Scholarships in Senior Year", 76 min) | Follows `research-260923-1837-webinar-trailer-reel.md`

## Result

`scripts/output/trailer/514db214/reel-9x16.mp4` (gitignored)

| Property | Value |
|---|---|
| Format | 1080×1920, 30 fps, H.264 + AAC 48 kHz, 62.7 s, 47 MB |
| Loudness | −14.7 LUFS integrated, −1.4 dBFS peak |
| Structure | 3 segments, every cut on a sentence boundary snapped to a pause |
| Captions | word-by-word: active word yellow with a scale pop; teal hook card for the first 3.5 s; "Watch the full webinar" for the last 3 s |
| Source | Zoom `active_speaker` file: camera only, so no slides; the centred crop also drops Zoom's name tag and clock |

Hook title: "Financial Aid Basics Parents Must Know"

Segments:
1. Need-based vs merit-based aid.
2. The aid-office formula. It says "this fundamental equation", which refers to a slide the reel does not show.
3. Applications open October 1st.

Cost:
- Bedrock is about $0.026 per call. This run took two calls, one of which was a retry.
- Render takes about 30 s locally.
- Nothing was written to the prod DB: the Bedrock ledger was stubbed to a log line.
- The temporary S3 audio was deleted, even when the Transcribe call failed.

## How it runs

`uv run python scripts/debug/trailer_reel_local.py --job-id <uuid>`. Stages are cached, and `--from 5` re-styles the captions for free.

| Module | Job |
|---|---|
| `trailer_sentences.py` | Re-cuts Zoom cues into sentences with interpolated times (678 sentences, median 5.7 s, from 415 cues, median 10.6 s); `snap_to_pause` uses silencedetect |
| `trailer_select.py` | Haiku proposes sentence ranges in play order. The code shortens, drops and fits them to 45–65 s, then retries once with the reasons |
| `trailer_render.py` | One `-ss` input per segment + concat, 50 ms audio fades → 9:16 crop + libass captions + loudnorm |
| `trailer_words.py` | AWS Transcribe word timings of the ~60 s reel audio only |
| `trailer_captions.py` | ASS script: groups of at most 3 words, one event per word |
| `fonts/` | Inter Black / ExtraBold (OFL) for libass |

## Findings that changed the design

- **Zoom cues are useless as cut points.** They are about 10 s long and break mid-sentence. Haiku also cannot add up durations: 3 attempts all returned 40–97 s segments. The fixes:
  - The unit is now the sentence.
  - The arithmetic is done in code.
  - Over-long picks are shortened to their leading sentences.
- **Interpolated sentence times are off by about ±1 s.** Snapping each cut to the nearest pause fixed it: all 6 boundaries landed within 0.2 s of a pause, with no clipped words.
- **Haiku sometimes returns malformed JSON.** The existing retry covers it.
- **Haiku still picks lines that point at slides** ("this equation"). A human review step stays mandatory; see also the unresolved questions below.

## Blocker for production

IAM user `cmm.cloud` has no `transcribe:*` permission. For this run, the word timings came from local faster-whisper (`small.en`), run from the scratchpad and not added to the repo.

Production needs these on the ECS task role in cmm-infra:
- `transcribe:StartTranscriptionJob`
- `transcribe:GetTranscriptionJob`
- `transcribe:DeleteTranscriptionJob`

## Storage recommendation: both

- **S3 as the master copy**, at `video-pipeline/{job_id}/trailer/reel-9x16.mp4`. It is the file marketing downloads to post on IG, TikTok and Shorts, and it is what the admin preview plays.
- **Vimeo for the resource center.** Upload only on approval, unlisted, into a "Reels" folder. The site already embeds Vimeo (player, adaptive streaming, analytics); serving MP4 from S3 would mean building that ourselves.

## Production work left

1. Archive `active_speaker` alongside `source.mp4` **before** the recording is trashed (`publish_service.py:193`). Today it is lost.
2. Add trailer columns via Alembic 0130, a `--mode trailer` ECS task and a sweeper, following the captions pattern.
3. Add a Transcribe IAM policy.
4. Add a trailer panel to the admin UI: preview, segment list with flags, approve/regenerate, download.
5. Upload to Vimeo on approval.

## Unresolved questions

1. Try a Sonnet-class model for selection? Haiku follows length rules poorly and misses slide references.
2. The source is 720p, so the 9:16 crop is upscaled about 2.7×. It looks acceptable on a phone. Is it good enough for marketing?
3. What to do with webinars where the presenter's camera is off? The speaker file would be blank, so either fall back to the slide layout or skip the reel.
4. Caption style (yellow highlight, Inter Black, lower-middle position) and CTA wording need brand sign-off.
5. Is presenter consent needed for social use of their likeness?
