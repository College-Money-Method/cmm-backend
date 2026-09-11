# Transcript-led chaptering

Status: implemented, tested, verified on real recordings, live chapters updated.
Scope: `src/video_pipeline/` in cmm-backend, plus two display-only files in cmm-frontend.

## Problem

Chaptering read the slide deck as a table of contents. It is not one.

A CMM deck opens with a title slide, a presenter bio and an agenda inside its
first four minutes, and drops a heading-only slide mid-section whenever the
presenter changes emphasis — "Investment and Value" two minutes into "Preparing
a college financial plan". Every one of those became a chapter. The 82-minute
webinar on Vimeo `1225339276` got 11 chapters where 7 were wanted, and the
opening minutes were the worst of it: boundaries at 0s, 50s, 94s, 161s.

No property of the frame separates the two cases. The sub-heading slide and the
section divider are the same red rectangle with the same white heading. What
separates them is what the speaker is doing: a section starts when the topic
turns, a sub-heading lands mid-explanation.

## Approach

The transcript decides **where** sections are. Frames are read only in windows
around those boundaries, and only to supply the deck's own wording and a precise
timecode.

Two decisions were locked before implementation:

- **Windows only** — classify frames near transcript boundaries, not a long
  screen-share run.
- **Fall back to today's rules** — a failed or nonsense transcript pass publishes
  using the frames-only logic under a minimum-section-length floor. Never fail
  the job.

Guards fail towards `[]`, which the caller reads as "segment from the frames as
before". This asymmetry is deliberate: a wrong section list is worse than no
section list, because it *deletes* chapters the frames found correctly.

## What changed

New:

- `topic_segment.py` — one Bedrock text call over the transcript in ~20s blocks.
  Returns `Section(start, kind, label)` with kinds `content` / `introduction` /
  `tour` / `qna`. Every guard returns `[]`.
- `section_chapters.py` — windows (90s before a boundary, 45s after), nearest
  title card within the window, walk back to the card's first appearance, Q&A
  snapped onto its opening sentence within 120s.

Modified in place:

- `frame_classify.py` — a slide carrying a heading **and anything else** is a
  content slide. A bio and an agenda are content slides: they have a heading,
  but they also have the thing the heading introduces.
- `chapter_build.py` — `COINCIDENT_SECONDS = 30.0`. No two chapters land this
  close whatever produced them; the recurring-segment exemption cannot reach
  below it.
- `bedrock_client.py` — `parse_leading_object` reads the JSON object at the
  front of a reply and ignores trailing prose.
- `publish_service.py` — reads cues, records `SEGMENTING_TRANSCRIPT`, segments,
  narrows the frame download to the windows, falls back to every frame if the
  windows caught nothing.
- Frontend: `segmenting_transcript` stage label; `VideoJobChapter.source` doc.

## Defects found by running it, and fixed

1. **The classifier went chatty.** Tightening the prompt made the model append a
   paragraph of reasoning after its JSON. `json.loads` rejected the whole reply
   over trailing prose it did not need, so 10 frames failed a first attempt and
   2 of 262 were lost entirely. Fixed twice over: the prompt now says "no code
   fence, no sentence before or after it, no reasoning", and `bedrock_client`
   uses `raw_decode` so trailing prose costs nothing. Re-run: 0 parse failures,
   0 retries, 0 lost frames.

2. **Two chapters 5 seconds apart survived.** The title card "Resource Center
   Tour + Q&A" (1:00:34) and the tour it announces (1:00:39) are one boundary,
   not two. The recurring exemption had no lower bound. `COINCIDENT_SECONDS`
   folds the tour into the card while leaving Q&A, 163s later, intact.

3. **A repeated recurring kind silently deleted a real boundary.** The model
   returned two `tour` sections for a webinar that demos two parts of the
   product. Both got the fixed label "Resource center tour" and `dedupe` dropped
   the second — losing a genuine 6.7-minute section. Recurring segments happen
   once per webinar by definition, so a repeat is now demoted to `content` in
   `_thin` and keeps its own wording.

## Verification

Both routes were exercised read-only against real S3 artefacts before anything
was written to Vimeo. This is what surfaced all three defects at zero cost.

**Fallback route** — job `913aad35-74ff-4b47-89bb-a41e9a543d95`, 262 frames, no
VTT (URL-sourced audit run, so by design no transcript). 11 chapters became 7:

```
0:00     [intro]       Introduction
5:29     [title_card]  Pricing and aid today
20:05    [title_card]  Understanding need-based aid
35:08    [title_card]  Finding merit generosity
53:04    [title_card]  Preparing a college financial plan
1:00:34  [title_card]  Resource Center Tour + Q&A
1:03:17  [qna]         Q&A
```

**Transcript spine** — the spine had never run on real data, because the only
job that reached Vimeo has no VTT. Run instead against a real 38-minute
recording with 231 cues. That webinar is a live product demo with **no title
cards anywhere**, so the frames-only path could only ever find 3 chapters. The
transcript found 6:

```
0:00   [intro]  Introduction
4:56   [tour]   Resource center tour
14:15  [topic]  Counselor Hub: Admin Features
20:55  [topic]  Communications & Planning Calendar
24:56  [topic]  Analytics & Engagement Tracking
32:21  [qna]    Q&A
```

That list is now live: video `1225339276` was re-chaptered from 11 to the 7
above, Vimeo confirmed all 7, no title hit the 50-character cap, and the job row
holds the same 7. The job was not moved back through the state machine to do it
— `published` is terminal — so only the chapter half of `publish()` was re-run,
against a job whose state the sweeper never touches.

The classifier is not bit-identical across runs: three passes over the same 262
frames gave `content_slide` 54/57/58 and `screen_share_other` 61/60/59. The
title-card count was 5 every time and the chapter list was identical every time,
which is the part that matters — a heading-only slide is not a borderline call.

Tests: `uv run pytest tests/video_pipeline/` → 393 passed (347 before this
work). Frontend `tsc --noEmit` clean; `vitest run` → 218/218 tests passed.

## Not caused by this work

- Backend: the analytics test collection failure is pre-existing.
- Frontend: 42 suites report failed, all `.claude/` tooling and
  `tests/mobile-qa` Playwright specs that collect no tests. Nothing under
  `app/`. Every actual test passes.

## Rejected

A test asserting that a demoted repeat is then held to the content floor. The
pair is `TOUR@0 → CONTENT@100`, and `TOUR` on the left genuinely earns the
existing exemption — identical in shape to any real content section shortly
after a tour, which is deliberately allowed and separately tested. Forcing the
floor there would have meant demotion bookkeeping in `_thin` to satisfy a case
that has not occurred. `COINCIDENT_SECONDS` still catches anything truly
adjacent.

## Known, unchanged

`publish_service.py:172` persists the chapter list it **built**, not the list
Vimeo **confirmed**. Vimeo caps a chapter title at exactly 50 characters, so the
job row and admin page can show a longer title than the video carries. Left
alone deliberately: the built list carries `source` and `confidence`, which
Vimeo's read-back does not, so storing the confirmed list would strip the two
fields the admin page exists to show. Reconciling properly means merging the two.

## Unresolved questions

1. Merge the confirmed titles onto the built list so the admin page cannot
   disagree with the video? Small, but it changes what a published row means.
2. `min_seconds=240.0` and the 90s/45s window are a first guess tuned on two
   recordings. Worth revisiting once more Zoom-sourced jobs have run with a
   transcript — so far exactly one real recording has exercised the spine.
3. Nothing is committed in either repo. Commit scope and message?
