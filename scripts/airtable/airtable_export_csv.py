#!/usr/bin/env python3
"""
Export all Airtable base data to CSV files (one file per table).

Pyairtable does not provide a built-in CSV export. This script uses the same
API as your other Airtable scripts: it fetches records via pyairtable and
writes them to CSV with Python's csv module.

Prerequisites:
  - .env with AIRTABLE_API_KEY and AIRTABLE_BASE_ID

Usage (from project root):
  uv run python scripts/airtable_export_csv.py
  uv run python scripts/airtable_export_csv.py --output-dir ./exports
  uv run python scripts/airtable_export_csv.py --tables "Schools" "Workshops"
  uv run python scripts/airtable_export_csv.py --all-tables --output-dir ./csv_exports
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.airtable_schema_to_postgres import (
    Api,
    fetch_all_records,
    get_all_table_names,
)


def _cell_to_str(value: Any) -> str:
    """Convert an Airtable cell value to a string for CSV."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        # Linked records: list of "recXXX" IDs, or list of strings/objects
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, (dict, list)):
                parts.append(json.dumps(item))
            else:
                parts.append(str(item))
        return ",".join(parts)
    if isinstance(value, dict):
        return json.dumps(value)
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _records_to_csv_path(
    table_name: str,
    records: list[dict[str, Any]],
    output_dir: Path,
    filename_sanitize: bool = True,
) -> Path:
    """
    Write Airtable records to a CSV file. Returns the path of the written file.
    Uses id, createdTime, then all field names (union across records) as columns.
    """
    if not records:
        # Empty table: still write a CSV with header only (id, createdTime)
        field_names = []
    else:
        field_names = sorted(
            set(
                key
                for rec in records
                for key in rec.get("fields", {}).keys()
            )
        )

    header = ["id", "createdTime"] + field_names

    if filename_sanitize:
        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in table_name)
        safe_name = safe_name.strip() or "table"
        out_name = f"{safe_name}.csv"
    else:
        out_name = f"{table_name}.csv"

    out_path = output_dir / out_name
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore", restval="")
        writer.writeheader()
        for rec in records:
            row = {
                "id": rec.get("id", ""),
                "createdTime": rec.get("createdTime", ""),
            }
            fields = rec.get("fields", {})
            for fn in field_names:
                row[fn] = _cell_to_str(fields.get(fn))
            writer.writerow(row)

    return out_path


def run_export(
    table_names: list[str] | None,
    api_key: str,
    base_id: str,
    output_dir: Path,
    all_tables: bool = False,
) -> dict[str, Path]:
    """
    Fetch Airtable data and write one CSV per table. Returns {table_name: output_path}.
    """
    if Api is None:
        raise RuntimeError("pyairtable is required. Install with: uv sync / pip install pyairtable")

    api = Api(api_key)
    if all_tables:
        table_names = get_all_table_names(api, base_id)
        if not table_names:
            raise RuntimeError("Base has no tables or schema could not be loaded")
    if not table_names:
        raise ValueError("No tables. Use --tables or --all-tables.")

    records_by_table = fetch_all_records(api, base_id, table_names)
    result: dict[str, Path] = {}
    for name in table_names:
        records = records_by_table.get(name, [])
        path = _records_to_csv_path(name, records, output_dir)
        result[name] = path
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Export Airtable base data to CSV files (one per table)",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        default=None,
        help="Airtable table names to export. Omit when using --all-tables.",
    )
    parser.add_argument(
        "--all-tables",
        action="store_true",
        help="Export all tables in the base (default if --tables not given and base has tables).",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        type=Path,
        default=Path("airtable_csv_exports"),
        help="Directory to write CSV files (default: airtable_csv_exports)",
    )
    args = parser.parse_args()

    from src.config import settings

    if not settings.airtable_api_key or not settings.airtable_base_id:
        print("Error: AIRTABLE_API_KEY and AIRTABLE_BASE_ID must be set in .env", file=sys.stderr)
        return 1

    # If neither --tables nor --all-tables, default to --all-tables
    if not args.tables and not args.all_tables:
        args.all_tables = True

    try:
        paths = run_export(
            args.tables if not args.all_tables else None,
            settings.airtable_api_key,
            settings.airtable_base_id,
            args.output_dir.resolve(),
            all_tables=args.all_tables,
        )
        for name, path in sorted(paths.items()):
            print(f"{name} -> {path}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        raise


if __name__ == "__main__":
    sys.exit(main())
