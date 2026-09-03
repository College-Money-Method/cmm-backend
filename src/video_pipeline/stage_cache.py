"""On-disk staging for pipeline runs.

Each stage writes its output to a known path and is skipped when that path
already exists. The two Bedrock stages are pure functions of their input, so
caching the ffmpeg work turns prompt iteration from a ten-minute loop into a
five-second one. The ECS task reuses the JSON helpers for its S3 staging.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Stage:
    number: int
    name: str
    output: Path


def load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def should_run(
    stage: Stage, *, from_stage: int, to_stage: int, force: bool
) -> bool:
    """True when this stage needs to execute rather than reuse its cached output."""
    if stage.number < from_stage or stage.number > to_stage:
        return False
    if force:
        return True
    return not stage.output.exists()


def require_cached(stage: Stage) -> object:
    """Load a stage's cached output, exiting with guidance when it is absent."""
    if not stage.output.exists():
        sys.exit(
            f"stage {stage.number} ({stage.name}) has no cached output at "
            f"{stage.output} — run it first (drop --from, or pass "
            f"--from {stage.number})"
        )
    return load_json(stage.output)
