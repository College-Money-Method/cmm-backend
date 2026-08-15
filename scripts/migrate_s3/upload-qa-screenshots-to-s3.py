#!/usr/bin/env python3
"""Batch-upload a directory of QA screenshots to S3 under readable keys.

Usage:
    python scripts/migrate_s3/upload-qa-screenshots-to-s3.py <local-dir> <key-prefix> [--dry-run]

Example:
    python scripts/migrate_s3/upload-qa-screenshots-to-s3.py \\
        ~/WebstormProjects/cmm-frontend/plans/.../ba portal/assets/qa-reports/src-mobile-260811

Why not `upload_to_s3.py`: that script names objects by a hash of the source and also
renders 128/256/512px thumbnails. Both are wrong for QA evidence — a report needs
`before/phone-375/home.png` in the URL to be reviewable, and thumbnails of a
20,000px-tall full-page capture are useless. This script preserves the directory
layout as the key suffix and uploads originals only.

Keys default to the `portal/assets/` prefix because that is the prefix the bucket
already serves publicly; a new top-level prefix may not be covered by the bucket policy.

Prints a JSON manifest of {relative-path: public-s3-url} to stdout so a report generator
can consume it. URLs are the direct S3 form (not the CDN host), which stays valid if the
CDN domain changes.

Dependencies: boto3, python-dotenv (already in the project).
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from src.config import settings  # noqa: E402

# Uploads are network-bound; a modest pool keeps throughput high without exhausting
# the connection pool botocore allocates per client.
MAX_WORKERS = 8
UPLOAD_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def collect_files(root: Path) -> list[Path]:
    """Every image under *root*, recursively, sorted for stable output."""
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in UPLOAD_SUFFIXES)


def upload_one(s3_client, bucket: str, path: Path, root: Path, prefix: str) -> tuple[str, str]:
    """Upload one file, preserving its path under *root* as the key suffix."""
    rel = path.relative_to(root).as_posix()
    key = f"{prefix.rstrip('/')}/{rel}"
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    s3_client.put_object(
        Bucket=bucket,
        Key=key,
        Body=path.read_bytes(),
        ContentType=content_type,
        # Screenshots for a dated report never change once written.
        CacheControl="public, max-age=31536000, immutable",
    )
    url = f"https://{bucket}.s3.{settings.aws_region}.amazonaws.com/{key}"
    return rel, url


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local_dir", help="Directory of images to upload (walked recursively)")
    parser.add_argument("key_prefix", help="S3 key prefix, e.g. portal/assets/qa-reports/foo")
    parser.add_argument("--dry-run", action="store_true", help="List planned keys, upload nothing")
    parser.add_argument(
        "--manifest", help="Also write the JSON manifest to this path", default=None
    )
    args = parser.parse_args()

    root = Path(args.local_dir).expanduser().resolve()
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        sys.exit(1)

    files = collect_files(root)
    if not files:
        print(f"Error: no images found under {root}", file=sys.stderr)
        sys.exit(1)

    total_mb = sum(f.stat().st_size for f in files) / 1_048_576
    print(f"{len(files)} images, {total_mb:.1f} MB, prefix {args.key_prefix}", file=sys.stderr)

    if args.dry_run:
        for f in files:
            print(f"  {args.key_prefix.rstrip('/')}/{f.relative_to(root).as_posix()}")
        return

    if not settings.s3_bucket_name:
        print("Error: s3_bucket_name is not set in .env", file=sys.stderr)
        sys.exit(1)

    s3 = boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
    )

    manifest: dict[str, str] = {}
    failures: list[str] = []
    done = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(upload_one, s3, settings.s3_bucket_name, f, root, args.key_prefix): f
            for f in files
        }
        for future in as_completed(futures):
            path = futures[future]
            try:
                rel, url = future.result()
                manifest[rel] = url
                done += 1
                if done % 10 == 0 or done == len(files):
                    print(f"  {done}/{len(files)} uploaded", file=sys.stderr)
            except (BotoCoreError, ClientError, OSError) as exc:
                failures.append(f"{path}: {exc}")

    # Report failures loudly rather than emitting a manifest that silently omits files.
    if failures:
        print(f"\n{len(failures)} upload(s) FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  {f}", file=sys.stderr)

    output = json.dumps(dict(sorted(manifest.items())), indent=2)
    if args.manifest:
        Path(args.manifest).expanduser().write_text(output)
        print(f"Manifest written to {args.manifest}", file=sys.stderr)
    else:
        print(output)

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
