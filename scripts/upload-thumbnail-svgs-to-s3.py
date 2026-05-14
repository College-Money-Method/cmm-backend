#!/usr/bin/env python3
"""Upload rendered thumbnail SVGs to S3 under portal/assets/thumbnails/.

Usage:
    python scripts/upload-thumbnail-svgs-to-s3.py [--svg-dir /path/to/svgs]

Defaults to /tmp/svg-render/output for --svg-dir.
Files are uploaded as portal/assets/thumbnails/{slug}.svg with their
descriptive slug name (not a hash), so the key is stable and human-readable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).parent.parent))
from src.config import settings  # noqa: E402

S3_PREFIX = "portal/assets/thumbnails"


def upload_svg(s3_client, bucket: str, region: str, svg_path: Path) -> str:
    key = f"{S3_PREFIX}/{svg_path.name}"
    svg_path.read_bytes()  # validate readable
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=svg_path.read_bytes(),
        ContentType="image/svg+xml",
    )
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--svg-dir", default="/tmp/svg-render/output")
    args = parser.parse_args()

    svg_dir = Path(args.svg_dir)
    svg_files = sorted(svg_dir.glob("*.svg"))
    if not svg_files:
        print(f"No SVG files found in {svg_dir}", file=sys.stderr)
        sys.exit(1)

    if not settings.s3_bucket_name:
        print("Error: s3_bucket_name is not set in .env", file=sys.stderr)
        sys.exit(1)

    s3 = boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )

    results: list[tuple[str, str]] = []
    for svg_path in svg_files:
        try:
            url = upload_svg(s3, settings.s3_bucket_name, settings.aws_region, svg_path)
            results.append((svg_path.stem, url))
            print(f"✓ {svg_path.name} → {url}")
        except (BotoCoreError, ClientError, OSError) as exc:
            print(f"✗ {svg_path.name}: {exc}", file=sys.stderr)
            sys.exit(1)

    print(f"\nUploaded {len(results)}/{len(svg_files)} SVGs to s3://{settings.s3_bucket_name}/{S3_PREFIX}/")


if __name__ == "__main__":
    main()
