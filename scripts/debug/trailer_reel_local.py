"""Build a ~60 second trailer reel (landscape or 9:16) from an already-processed webinar job.

Read-only against production: it reads the job row, the job's transcript
artifact in S3 and the recording in Zoom, and writes nothing back — the one S3
object it creates (the reel audio Transcribe reads) is deleted straight after.
Everything else lands in `--out`.

    uv run python scripts/debug/trailer_reel_local.py --job-id <uuid>

Stages, each cached in `--out` and skipped when its output exists:
  1 inputs    job metadata + transcript cues (+ speaker.mp4 from Zoom if absent)
  2 select    Bedrock picks sentence runs              (selection.json)
  3 cut       snap cuts to pauses, join the segments   (cuts.json, reel-cut.mov)
  4 words     AWS Transcribe word timings of the reel  (words.json)
  5 captions  TikTok-style ASS captions                (reel-16x9.ass)
  6 render    camera/screen edit + burned captions     (reel-16x9.mp4)
              + loudnorm; the screen is the archived shared-screen original

`--orientation portrait` renders 9:16 instead (reel-9x16.*); both share stages
1-4. `--from 5` re-styles the captions without paying for Bedrock or Transcribe.
`--no-screen` keeps the whole reel on the presenter's camera.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from src.config import settings
from src.video_pipeline import bedrock_usage
from src.video_pipeline.archive_original import VIDEO_FILENAME
from src.video_pipeline.artifact_store import TRANSCRIPT_FILENAME, load_json_artifact
from src.video_pipeline.ffmpeg_ops import probe_duration, require_ffmpeg
from src.video_pipeline.reel_build import FONTS_DIR, presenter_label, snapped
from src.video_pipeline.stage_cache import (
    Stage,
    dump_json,
    load_json,
    require_cached,
    should_run,
)
from src.video_pipeline.trailer_captions import CTA_SECONDS, LAYOUTS, build_ass
from src.video_pipeline.trailer_render import (
    cut_segments,
    extract_audio,
    render_reel,
)
from src.video_pipeline.trailer_edit import SHRINK_SECONDS, plan_shots, screen_windows
from src.video_pipeline.trailer_select import Segment, Selection, select_segments
from src.video_pipeline.trailer_sentences import split_sentences
from src.video_pipeline.trailer_words import Word, transcribe_words
from src.video_pipeline.transcript import Cue

logger = logging.getLogger("trailer_reel_local")

# Zoom's camera-only rendition: no slides, so nothing on screen can leak.
SPEAKER_RECORDING_TYPES = ("active_speaker", "speaker_view")
_SPEAKER_PREFIX = re.compile(r"^[^:.?!]{1,80}:\s+")
ASPECTS = {"landscape": "16x9", "portrait": "9x16"}


def main() -> int:
    args = _parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    # httpx logs every request URL, and Transcribe's result URL is presigned.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    require_ffmpeg()
    _keep_bedrock_ledger_local()

    out: Path = args.out or Path("scripts/output/trailer") / str(args.job_id)[:8]
    out.mkdir(parents=True, exist_ok=True)
    aspect = ASPECTS[args.orientation]
    stages = {
        1: Stage(1, "inputs", out / "inputs.json"),
        2: Stage(2, "select", out / "selection.json"),
        3: Stage(3, "cut", out / "reel-cut.mov"),
        4: Stage(4, "words", out / "words.json"),
        5: Stage(5, "captions", out / f"reel-{aspect}.ass"),
        6: Stage(6, "render", out / f"reel-{aspect}.mp4"),
    }

    def run(n: int) -> bool:
        return should_run(stages[n], from_stage=args.from_stage, to_stage=args.to_stage,
                          force=args.force)

    if run(1):
        dump_json(stages[1].output, _load_inputs(args.job_id))
    inputs = require_cached(stages[1])
    speaker = out / "speaker.mp4"
    if args.to_stage >= 3 and not speaker.exists():
        _download_speaker_video(inputs["zoom_recording_uuid"], speaker)
    screen = out / "screen.mp4"
    if args.screen and args.to_stage >= 3 and not screen.exists():
        _download_screen_video(inputs, screen)

    if run(2):
        sentences = split_sentences([Cue(**cue) for cue in inputs["cues"]])
        logger.info("%d cues → %d sentences", len(inputs["cues"]), len(sentences))
        selection = select_segments(sentences, inputs["chapters"], inputs["title"])
        dump_json(stages[2].output, selection.as_dict())
    if args.to_stage < 3:
        return _summary(stages)
    selection = Selection.from_dict(require_cached(stages[2]))

    if run(3):
        offset = float(inputs["trim_offset"])
        cuts = [snapped(speaker, segment, offset) for segment in selection.segments]
        dump_json(out / "cuts.json", [asdict(cut) for cut in cuts])
        cut_segments(speaker, cuts, stages[3].output, offset=offset)
    reel_duration = probe_duration(stages[3].output)

    if run(4):
        audio = extract_audio(stages[3].output, out / "reel-audio.flac")
        words = transcribe_words(audio, f"video-pipeline/{args.job_id}/trailer")
        dump_json(stages[4].output, [word.as_dict() for word in words])
    words = [Word(**w) for w in require_cached(stages[4])] if args.to_stage >= 5 else []
    if words and (out / "cuts.json").exists():
        _check_boundaries([Segment(**cut) for cut in load_json(out / "cuts.json")], words)

    cuts = [Segment(**cut) for cut in load_json(out / "cuts.json")]
    windows = []
    if args.screen:
        # The end card plays over the camera, clear of the last transition.
        hold = CTA_SECONDS + SHRINK_SECONDS if args.cta else 0.0
        windows = screen_windows([cut.duration for cut in cuts], hold_end=hold)
        logger.info("Screen windows: %s", ", ".join(f"{a:.1f}-{b:.1f}s" for a, b in windows))

    if run(5):
        stages[5].output.write_text(
            build_ass(words, duration=reel_duration, layout=LAYOUTS[args.orientation],
                      title=selection.hook_title, cta=args.cta,
                      presenter=presenter_label([Cue(**cue) for cue in inputs["cues"]]), screen=windows),
            encoding="utf-8",
        )
    if run(6):
        render_reel(stages[3].output, stages[5].output, stages[6].output,
                    orientation=args.orientation, fonts_dir=args.fonts_dir, crop_x=args.crop_x,
                    camera=speaker, screen=screen if windows else None,
                    shots=plan_shots(cuts, windows, offset=float(inputs["trim_offset"])))
    return _summary(stages)




def _check_boundaries(cuts: list[Segment], words: list[Word]) -> None:
    """Warn when a segment does not start and end on the words its sentences do.

    The recogniser hears what the reel actually contains, so a clipped first or
    last word shows up here even though the cut looked right on the transcript.
    """
    def norm(text: str) -> str:
        return re.sub(r"[^a-z0-9]", "", text.lower())

    at = 0.0
    for n, cut in enumerate(cuts, start=1):
        # By end time: a recogniser stretches the first word after a pause back
        # into the silence, across the join, but never the end of a word.
        inside = [w for w in words if at < w.end <= at + cut.duration + 0.1]
        at += cut.duration
        text = _SPEAKER_PREFIX.sub("", cut.text).split()
        if not inside or not text:
            logger.warning("Segment %d: no words heard", n)
            continue
        heard = (norm(inside[0].text), norm(inside[-1].text))
        expected = (norm(text[0]), norm(text[-1]))
        if heard == expected:
            logger.info("Segment %d: starts on %r, ends on %r ✓", n, text[0], text[-1])
        else:
            logger.warning("Segment %d: expected %r…%r, heard %r…%r — cut may clip speech",
                           n, text[0], text[-1], inside[0].text, inside[-1].text)



def _keep_bedrock_ledger_local() -> None:
    """Swap the Bedrock spend ledger for a log line.

    `call_json` records every invocation into the production database. This run
    promised no database writes, so the tokens are printed instead of stored.
    """
    def record(invoke_type: str, model_id: str, input_tokens: int, output_tokens: int) -> None:
        cost = bedrock_usage.cost_usd(input_tokens, output_tokens, model_id)
        logger.info("Bedrock %s: %d in / %d out tokens ≈ $%s (not recorded)",
                    invoke_type, input_tokens, output_tokens, cost)

    bedrock_usage.record = record


def _load_inputs(job_id: uuid.UUID) -> dict:
    """Everything the later stages need from production, read once."""
    import src.db.models  # noqa: F401, PLC0415 - registers every mapper
    from src.db.base import get_session_factory  # noqa: PLC0415
    from src.video_pipeline.models import WebinarVideoJob  # noqa: PLC0415
    from src.workshops.models import Webinar  # noqa: PLC0415

    with get_session_factory()() as db:
        job = db.get(WebinarVideoJob, job_id)
        if job is None:
            sys.exit(f"no webinar video job {job_id}")
        if not job.frames_prefix or job.trim_offset_seconds is None:
            sys.exit(f"job {job_id} has no transcript artifact or trim offset yet")
        webinar = db.get(Webinar, job.webinar_id) if job.webinar_id else None
        inputs = {
            "title": webinar.webinar_name if webinar else "",
            "zoom_recording_uuid": job.zoom_recording_uuid,
            "trim_offset": float(job.trim_offset_seconds),
            "chapters": job.chapters or [],
            "frames_prefix": job.frames_prefix,
            "archive_key": job.archive_key,
        }
    # Already re-based onto the trimmed clock: the clock the chapters use.
    inputs["cues"] = load_json_artifact(inputs["frames_prefix"], TRANSCRIPT_FILENAME)
    logger.info("Job %s: %r, %d cues, trim offset %.3fs", job_id, inputs["title"],
                len(inputs["cues"]), inputs["trim_offset"])
    return inputs


def _download_speaker_video(recording_uuid: str, dest: Path) -> None:
    """Fetch Zoom's camera-only file. Every Zoom file shares one start time, so
    the trim offset applies to it unchanged."""
    from src.integrations.zoom import (  # noqa: PLC0415
        get_recording,
        recording_access_token,
    )
    from src.video_pipeline.zoom_recording_fetch import (  # noqa: PLC0415
        _download,
        _files,
    )

    payload = get_recording(recording_uuid)
    if payload is None:
        sys.exit("recording is no longer in Zoom — pass a local speaker.mp4 in --out")
    files = [f for f in _files(payload)
             if str(f.get("recording_type", "")).lower() in SPEAKER_RECORDING_TYPES
             and str(f.get("file_type", "")).upper() == "MP4"]
    if not files:
        sys.exit("Zoom has no speaker-only rendition of this recording")
    logger.info("Downloading %s (%d MB)…", files[0]["recording_type"],
                int(files[0].get("file_size", 0)) // 1_000_000)
    _download(files[0]["download_url"], recording_access_token(), dest)


def _download_screen_video(inputs: dict, dest: Path) -> None:
    """Fetch the archived original: the shared-screen rendition the job published.

    Zoom trashes its copy after publishing, so the S3 archive is the lasting
    source. Glacier Instant Retrieval is a plain GET.
    """
    from src.storage.s3_client import s3_client  # noqa: PLC0415

    prefix = inputs.get("archive_key")
    if not prefix:
        sys.exit("no archive_key in inputs.json — re-run stage 1 (--from 1 --to 1 --force), "
                 "or pass --no-screen")
    logger.info("Downloading the archived shared-screen original…")
    s3_client().download_file(settings.s3_bucket_name, f"{prefix}{VIDEO_FILENAME}", str(dest))


def _summary(stages: dict[int, Stage]) -> int:
    selection_path = stages[2].output
    if selection_path.exists():
        selection = load_json(selection_path)
        print(f"\nHook title: {selection['hook_title']}  ({selection['total_seconds']}s)")
        for i, seg in enumerate(selection["segments"], start=1):
            flags = f"  [{', '.join(seg['flags'])}]" if seg["flags"] else ""
            print(f"  {i}. {seg['start']:8.2f}-{seg['end']:8.2f}  {seg['why']}{flags}")
            print(f"     \"{seg['text'][:160]}\"")
    for stage in stages.values():
        state = "ok" if stage.output.exists() else "--"
        print(f"  [{state}] {stage.number} {stage.name:<9} {stage.output}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job-id", type=uuid.UUID, required=True)
    parser.add_argument("--out", type=Path, help="default: scripts/output/trailer/<id8>")
    parser.add_argument("--from", dest="from_stage", type=int, default=1)
    parser.add_argument("--to", dest="to_stage", type=int, default=6)
    parser.add_argument("--force", action="store_true", help="re-run stages in range")
    parser.add_argument("--orientation", choices=tuple(ASPECTS), default="landscape",
                        help="landscape keeps the camera frame's native quality; portrait "
                             "crops 9:16 for Reels/TikTok/Shorts (default landscape)")
    parser.add_argument("--crop-x", type=float, default=0.5,
                        help="portrait crop position, 0 left … 1 right (default centred)")
    parser.add_argument("--cta", default="Watch the full webinar",
                        help="end card text; empty to omit")
    parser.add_argument("--fonts-dir", type=Path, default=FONTS_DIR)
    parser.add_argument("--screen", action=argparse.BooleanOptionalAction, default=True,
                        help="cut over to the shared screen in every segment after the "
                             "first (default on)")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
