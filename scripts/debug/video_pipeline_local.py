"""Run the webinar video pipeline locally against a downloaded recording.

Validates the risky middle of the pipeline — the crop, the candidate count, the
trim point, the frame classification — with no database, no ECS, no webhook, and
no writes to Vimeo. Every stage caches its output, so iterating on a prompt does
not re-extract frames.

    uv run python scripts/debug/video_pipeline_local.py \
        scripts/input/sample.mp4 scripts/input/sample.vtt --out scripts/output/vp

Stages: 1 cues, 2 trim point, 3 trim, 4 frames, 5 classify, 6 chapters, 7 report.
Only 2 and 5 call Bedrock, so `--trim-offset 0 --to 4` checks the crop for free.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from src.video_pipeline.chapter_build import Chapter, build_chapters
from src.video_pipeline.ffmpeg_ops import (
    Candidate,
    FfmpegError,
    build_sampling_filter,
    probe_duration,
    require_ffmpeg,
    sample_distinct_frames,
    trim_stream_copy,
)
from src.video_pipeline.frame_classify import (
    ERROR,
    TITLE_CARD,
    Classified,
    classify_frames,
)
from src.video_pipeline.report_html import render_report
from src.video_pipeline.stage_cache import (
    Stage,
    dump_json,
    load_json,
    require_cached,
    should_run,
)
from src.video_pipeline.trim_point_detect import detect_trim_point
from src.video_pipeline.transcript import Cue, format_timestamp, load_cues, rebase

logger = logging.getLogger("video_pipeline_local")

# A 90-minute webinar should yield tens to low hundreds of distinct states.
# Thousands means a changing overlay survived the crop.
CANDIDATE_SANITY_CEILING = 600


def _run_stage(stage: Stage, args: argparse.Namespace) -> bool:
    return should_run(
        stage,
        from_stage=args.from_stage,
        to_stage=args.to_stage,
        force=args.force,
    )


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    video: Path = args.video
    if not video.exists():
        sys.exit(f"video not found: {video}")
    if args.vtt and not args.vtt.exists():
        sys.exit(f"transcript not found: {args.vtt}")

    out: Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    frames_dir = out / "frames"

    stages = {
        1: Stage(1, "cues", out / "cues.json"),
        2: Stage(2, "trim point", out / "trim.json"),
        3: Stage(3, "trim", out / "trimmed.mp4"),
        4: Stage(4, "frames", out / "candidates.json"),
        5: Stage(5, "classify", out / "classified.json"),
        6: Stage(6, "chapters", out / "chapters.json"),
        7: Stage(7, "report", out / "report.html"),
    }

    try:
        require_ffmpeg()
    except FfmpegError as exc:
        sys.exit(str(exc))

    # ---- 1. cues -----------------------------------------------------------
    cues: list[Cue] = []
    if _run_stage(stages[1], args):
        if args.vtt:
            cues = load_cues(args.vtt)
            dump_json(stages[1].output, [cue.as_dict() for cue in cues])
            logger.info("stage 1: parsed %d cues", len(cues))
        else:
            dump_json(stages[1].output, [])
            logger.warning("stage 1: no transcript given — trim and Q&A degraded")
    elif stages[1].output.exists():
        cues = [Cue(**item) for item in load_json(stages[1].output)]  # type: ignore[arg-type]
        logger.info("stage 1: reusing %d cues", len(cues))

    # ---- 2. trim point -----------------------------------------------------
    if args.trim_offset is not None:
        trim = {
            "offset": args.trim_offset,
            "cue_index": -1,
            "cue_start": args.trim_offset,
            "reason": "supplied on the command line",
            "fallback": False,
        }
        dump_json(stages[2].output, trim)
        logger.info("stage 2: using --trim-offset %.2fs", args.trim_offset)
    elif _run_stage(stages[2], args):
        point = detect_trim_point(cues)
        trim = point.as_dict()
        dump_json(stages[2].output, trim)
        logger.info(
            "stage 2: offset %.2fs%s",
            point.offset,
            " (FALLBACK — no trim applied)" if point.fallback else "",
        )
    else:
        trim = require_cached(stages[2])  # type: ignore[assignment]
        logger.info("stage 2: reusing offset %.2fs", float(trim["offset"]))

    offset = float(trim["offset"])
    if args.to_stage < 3:
        return _finish(out, stages, args)

    # ---- 3. trim -----------------------------------------------------------
    trimmed = stages[3].output
    if _run_stage(stages[3], args):
        trim_stream_copy(video, trimmed, offset)
        logger.info(
            "stage 3: trimmed to %s (%s)",
            trimmed.name,
            format_timestamp(probe_duration(trimmed)),
        )
    elif not trimmed.exists():
        require_cached(stages[3])
    if args.to_stage < 4:
        return _finish(out, stages, args)

    duration = probe_duration(trimmed)

    # ---- 4. frames ---------------------------------------------------------
    crop_w = None if args.no_crop else args.crop_w
    crop_h = None if args.no_crop else args.crop_h
    if _run_stage(stages[4], args):
        logger.info(
            "stage 4: filter = %s",
            build_sampling_filter(
                fps=args.fps, width=args.width, crop_w=crop_w, crop_h=crop_h
            ),
        )
        candidates = sample_distinct_frames(
            trimmed, frames_dir,
            fps=args.fps, width=args.width, crop_w=crop_w, crop_h=crop_h,
        )
        dump_json(stages[4].output, [c.as_dict() for c in candidates])
    else:
        raw = require_cached(stages[4])
        candidates = [
            Candidate(
                index=int(item["index"]),
                timestamp=float(item["timestamp"]),
                path=frames_dir / str(item["file"]),
            )
            for item in raw  # type: ignore[union-attr]
        ]
    logger.info("stage 4: %d distinct states", len(candidates))
    if len(candidates) > CANDIDATE_SANITY_CEILING:
        logger.warning(
            "%d candidates is far more than a slide deck produces — a changing "
            "overlay probably survived the crop. Open a frame and check before "
            "spending model calls on stage 5.",
            len(candidates),
        )
    if args.to_stage < 5:
        return _finish(out, stages, args)
    if not candidates:
        sys.exit("no candidate frames — nothing to classify")

    # ---- 5. classify -------------------------------------------------------
    if _run_stage(stages[5], args):
        classified = classify_frames(candidates, concurrency=args.concurrency)
        dump_json(stages[5].output, [item.as_dict() for item in classified])
    else:
        classified = [
            Classified(
                index=int(item["index"]),
                timestamp=float(item["timestamp"]),
                file=str(item["file"]),
                type=str(item["type"]),
                heading=str(item.get("heading") or ""),
                error=str(item.get("error") or ""),
            )
            for item in require_cached(stages[5])  # type: ignore[union-attr]
        ]
    counts: dict[str, int] = {}
    for item in classified:
        counts[item.type] = counts.get(item.type, 0) + 1
    logger.info(
        "stage 5: %s",
        ", ".join(f"{key}={value}" for key, value in sorted(counts.items())),
    )
    if args.to_stage < 6:
        return _finish(out, stages, args)

    # ---- 6. chapters -------------------------------------------------------
    rebased = rebase(cues, offset) if cues else []
    chapters, runs = build_chapters(
        classified,
        cues=rebased,
        duration=duration,
        tour_min_seconds=args.tour_min,
        max_chapters=args.max_chapters,
    )
    dump_json(stages[6].output, [chapter.as_dict() for chapter in chapters])
    dump_json(out / "runs.json", [
        {"type": run.type, "start": run.start, "end": run.end, "heading": run.heading}
        for run in runs
    ])
    print(f"\n{len(chapters)} chapters from {len(candidates)} candidates:\n")
    for chapter in chapters:
        print(f"  {format_timestamp(chapter.timecode):>8}  {chapter.title}")
    print()
    if args.to_stage < 7:
        return _finish(out, stages, args)

    # ---- 7. report ---------------------------------------------------------
    report = render_report(
        out_dir=out,
        source_name=video.name,
        trim=trim,  # type: ignore[arg-type]
        frames=[item.as_dict() for item in classified],
        chapters=chapters,
    )
    logger.info("stage 7: %s", report)
    return _finish(out, stages, args, report=report)


def _finish(
    out: Path,
    stages: dict[int, Stage],
    args: argparse.Namespace,
    report: Path | None = None,
) -> int:
    if report and args.open_report:
        subprocess.run(["open", str(report)], check=False)
    elif report:
        print(f"report: {report}")
    else:
        print(f"stopped after stage {args.to_stage}; outputs in {out}")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("video", type=Path, help="downloaded Zoom mp4")
    parser.add_argument(
        "vtt", type=Path, nargs="?", help="Zoom transcript VTT (optional)"
    )
    parser.add_argument(
        "--out", type=Path, default=Path("scripts/output/vp"),
        help="output directory (default: scripts/output/vp)",
    )
    parser.add_argument(
        "--from", dest="from_stage", type=int, default=1, metavar="N",
        help="re-run from stage N; earlier stages load from cache",
    )
    parser.add_argument(
        "--to", dest="to_stage", type=int, default=7, metavar="N",
        help="stop after stage N",
    )
    parser.add_argument(
        "--force", action="store_true", help="re-run stages even when cached"
    )
    parser.add_argument(
        "--trim-offset", type=float, metavar="SEC",
        help="skip detection and cut here; 0 samples the untrimmed video",
    )
    parser.add_argument("--fps", type=float, default=2.0, help="sampling rate")
    parser.add_argument("--width", type=int, default=960, help="scale width")
    parser.add_argument(
        "--crop-w", type=float, default=0.83, help="kept width fraction"
    )
    parser.add_argument(
        "--crop-h", type=float, default=0.88, help="kept height fraction"
    )
    parser.add_argument(
        "--no-crop", action="store_true",
        help="sample without cropping, to see what the overlays do",
    )
    parser.add_argument(
        "--concurrency", type=int, default=4, help="parallel vision calls"
    )
    parser.add_argument(
        "--tour-min", type=float, default=120.0,
        help="seconds of screen share that count as the tour",
    )
    parser.add_argument("--max-chapters", type=int, default=40)
    parser.add_argument(
        "--open", dest="open_report", action="store_true", help="open the report"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(main())
