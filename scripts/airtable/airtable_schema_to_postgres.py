#!/usr/bin/env python3
"""
Airtable → Postgres schema inference and code generation.

Fetches data from multiple Airtable tables, infers column types and relationships
(linked records = arrays of "rec..." IDs), and generates (and optionally runs)
Postgres DDL for Supabase.

Fetching tables:
  - Use --tables "Table1" "Table2" to process only those tables.
  - Use --all-tables to fetch every table in the base (via the base schema API).
    When you use --all-tables, related tables are included automatically and
    link targets are resolved from the schema (linked_table_id), so relations
    are correct even when a linked table is empty.

Relation inference:
  - Airtable linked-record fields appear in the API as lists of strings like
    ['recXYZ123', 'recABC456']. We detect these and create junction tables
    (many-to-many) with foreign keys to both sides.
  - When the base schema is available (always loaded, one API call), we use
    it to resolve which table each link points to (exact linked_table_id),
    so relations are correct even if you only pass a subset of tables.

Primary keys and relations:
  - Each table gets id UUID PRIMARY KEY (you control it) and airtable_id TEXT UNIQUE (for sync).
  - All FKs and junction tables reference id (UUID), not airtable_id.
  - In your data-pull script: use airtable_id to find/create rows and to resolve
    Airtable link fields to our UUIDs, then write relation rows using those UUIDs.
  - Rollup/lookup fields are skipped when schema is available; add as views later if needed.
  - Column names are truncated to 63 chars (Postgres limit) and de-duplicated.
  - .env with AIRTABLE_API_KEY and AIRTABLE_BASE_ID
  - Optional: DATABASE_URL (Postgres connection string) to apply generated SQL
    (requires psycopg2: pip install psycopg2-binary)

Usage (from project root). Recommended flow: generate schema → db reset → pull data:
  uv run python scripts/airtable_schema_to_postgres.py --all-tables -o supabase/migrations/20250312000000_airtable_schema.sql
  supabase db reset
  uv run python scripts/airtable_pull_data.py --all-tables

  uv run python scripts/airtable_schema_to_postgres.py --all-tables
  uv run python scripts/airtable_schema_to_postgres.py --tables "Table1" "Table2"
  uv run python scripts/airtable_schema_to_postgres.py --tables "Schools" -o migration.sql
  uv run python scripts/airtable_schema_to_postgres.py --all-tables --apply  # if DATABASE_URL set

From FastAPI (e.g. background task)::

  from scripts.airtable_schema_to_postgres import run_inference_and_generate
  inferred, junctions, sql = run_inference_and_generate(
      None, settings.airtable_api_key, settings.airtable_base_id, all_tables=True
  )
  # Or with specific tables: run_inference_and_generate(["Schools", "Workshops"], ...)
  # Then write sql to a migration file or run_sql(sql, settings.database_url)
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Project root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from pyairtable import Api
except ImportError:
    Api = None  # type: ignore[misc, assignment]

# Optional: use base schema for exact link targets (linked_table_id)
try:
    from pyairtable.models.schema import BaseSchema
except ImportError:
    BaseSchema = None  # type: ignore[misc, assignment]


# -----------------------------------------------------------------------------
# Naming: Airtable "Title Case" / spaces → strict snake_case
# -----------------------------------------------------------------------------

# Postgres identifier max length (63) to avoid "column specified more than once"
POSTGRES_IDENTIFIER_MAX_LENGTH = 63


def to_snake_case(name: str, max_length: int | None = 63) -> str:
    """Convert Airtable table/field name to snake_case for Postgres. Truncates to max_length (default 63)."""
    if not name or not isinstance(name, str):
        return "unknown"
    s = re.sub(r"[\s\-]+", "_", name)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", s)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", s)
    s = s.lower()
    s = re.sub(r"[^a-z0-9_]", "", s)
    s = s or "unknown"
    if max_length is not None and len(s) > max_length:
        s = s[:max_length].rstrip("_") or "field"
    return s


# -----------------------------------------------------------------------------
# Phase 1: Data fetching and type / relation inference
# -----------------------------------------------------------------------------

# Airtable record IDs: "rec" prefix + alphanumeric (length varies in practice)
REC_ID_PREFIX = "rec"
REC_ID_PATTERN = re.compile(r"^rec[A-Za-z0-9]{8,20}$")


def is_airtable_record_id(value: Any) -> bool:
    if isinstance(value, str):
        return bool(REC_ID_PATTERN.match(value))
    return False


def looks_like_linked_record_field(values: list[Any]) -> bool:
    """
    A field is treated as a linked-record (relationship) field if every non-null
    value is a list of strings that look like Airtable record IDs (rec...).
    """
    if not values:
        return False
    for v in values:
        if v is None:
            continue
        if not isinstance(v, list):
            return False
        for item in v:
            if not is_airtable_record_id(item):
                return False
    return True


def infer_pg_type(values: list[Any], field_name: str) -> str:
    """
    Infer Postgres type from a list of sample values (all values for this field).
    Used only for non-linked fields; linked fields are handled by junction tables.
    """
    non_null = [v for v in values if v is not None and v != ""]
    if not non_null:
        return "TEXT"

    bool_count = sum(1 for v in non_null if isinstance(v, bool))
    int_count = sum(1 for v in non_null if isinstance(v, int) and not isinstance(v, bool))
    float_count = sum(1 for v in non_null if isinstance(v, (int, float)) and isinstance(v, float))
    str_count = sum(1 for v in non_null if isinstance(v, str))
    list_count = sum(1 for v in non_null if isinstance(v, list))
    dict_count = sum(1 for v in non_null if isinstance(v, dict))

    if list_count == len(non_null):
        # List of non-rec IDs: could be JSON array or text array
        first = next(v for v in non_null if v)
        if first and all(is_airtable_record_id(x) for x in first):
            return "LINKED_RECORDS"  # special marker
        return "JSONB"
    if dict_count == len(non_null):
        return "JSONB"
    if bool_count == len(non_null):
        return "BOOLEAN"
    if int_count == len(non_null) and float_count == 0:
        return "BIGINT"
    if (int_count + float_count) == len(non_null):
        return "DOUBLE PRECISION"
    if str_count == len(non_null):
        # Heuristic: ISO-ish datetime
        for v in non_null:
            if isinstance(v, str) and len(v) >= 19 and "T" in v and ("Z" in v or "+" in v or "-" in v[-6:]):
                return "TIMESTAMPTZ"
        return "TEXT"
    return "TEXT"


@dataclass
class InferredColumn:
    """A single column in the inferred schema."""
    snake_name: str
    pg_type: str
    is_linked_record: bool = False
    linked_target_table_snake: str | None = None  # for junction table target
    airtable_field_name: str | None = None  # original Airtable field name (for data pull)


@dataclass
class InferredTable:
    """Inferred schema for one Airtable table."""
    airtable_name: str
    table_snake: str
    records: list[dict[str, Any]]  # raw records with 'id', 'createdTime', 'fields'
    columns: list[InferredColumn] = field(default_factory=list)
    # record_id -> set of record IDs that appear in this table (for resolving link targets)
    record_ids: set[str] = field(default_factory=set)


def fetch_all_records(api: Api, base_id: str, table_names: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Fetch all records for each given table name. Returns {table_name: [RecordDict, ...]}."""
    base = api.base(base_id)
    result: dict[str, list[dict[str, Any]]] = {}
    for name in table_names:
        table = base.table(name)
        records = table.all()
        result[name] = records
    return result


def get_all_table_names(api: Api, base_id: str) -> list[str]:
    """
    Fetch the base schema from Airtable and return all table names.
    Uses the standard Base Schema API (meta/bases/{id}/tables), not Enterprise.
    """
    base = api.base(base_id)
    schema = base.schema()
    return [t.name for t in schema.tables]


def infer_schema(
    table_names: list[str],
    records_by_table: dict[str, list[dict[str, Any]]],
    base_schema: BaseSchema | None = None,
) -> tuple[dict[str, InferredTable], dict[str, set[str]]]:
    """
    Two-pass inference:
      - Pass 1: infer column types and mark linked-record fields.
      - Pass 2: for each linked field, resolve which table the rec IDs belong to.

    Returns (inferred_tables by table_snake, record_ids_by_table_snake).
    """
    # Build record_id sets per table (by Airtable name, then we'll key by snake later)
    record_ids_by_airtable_name: dict[str, set[str]] = {}
    for name, records in records_by_table.items():
        ids = {r["id"] for r in records}
        record_ids_by_airtable_name[name] = ids

    inferred: dict[str, InferredTable] = {}
    for airtable_name, records in records_by_table.items():
        table_snake = to_snake_case(airtable_name)
        all_fields: dict[str, list[Any]] = defaultdict(list)
        for rec in records:
            for k, v in rec.get("fields", {}).items():
                all_fields[k].append(v)

        columns: list[InferredColumn] = []
        used_snake_names: set[str] = set()
        for field_name, values in all_fields.items():
            # Skip rollup/lookup: they derive from relations; add as views later if needed
            if base_schema is not None:
                try:
                    table_schema = base_schema.table(airtable_name)
                    field_schema = table_schema.field(field_name)
                    ftype = getattr(field_schema, "type", None)
                    if ftype in ("rollup", "multipleLookupValues"):
                        continue
                except (KeyError, TypeError, AttributeError):
                    pass
            snake = to_snake_case(field_name)
            # Ensure unique column name (truncation can create duplicates)
            base_snake = snake
            suffix = 1
            while snake in used_snake_names:
                suffix += 1
                extra = f"_{suffix}"
                snake = (base_snake[: 63 - len(extra)]).rstrip("_") + extra
            used_snake_names.add(snake)
            if looks_like_linked_record_field(values):
                columns.append(InferredColumn(snake_name=snake, pg_type="LINKED_RECORDS", is_linked_record=True, airtable_field_name=field_name))
            else:
                pg_type = infer_pg_type(values, field_name)
                if pg_type == "LINKED_RECORDS":
                    columns.append(InferredColumn(snake_name=snake, pg_type="LINKED_RECORDS", is_linked_record=True, airtable_field_name=field_name))
                else:
                    columns.append(InferredColumn(snake_name=snake, pg_type=pg_type, is_linked_record=False, airtable_field_name=field_name))

        inferred[table_snake] = InferredTable(
            airtable_name=airtable_name,
            table_snake=table_snake,
            records=records,
            columns=columns,
            record_ids=record_ids_by_airtable_name.get(airtable_name, set()),
        )

    # Resolve linked-record targets: which table do the rec IDs in this field belong to?
    table_snakes = list(inferred.keys())
    record_ids_by_snake: dict[str, set[str]] = {
        to_snake_case(name): s for name, s in record_ids_by_airtable_name.items()
    }

    for t in inferred.values():
        for col in t.columns:
            if not col.is_linked_record:
                continue
            # Collect all rec IDs that appear in this field across records (fields use Airtable names)
            rec_ids_in_field: set[str] = set()
            for rec in t.records:
                for fname, fval in rec.get("fields", {}).items():
                    if to_snake_case(fname) == col.snake_name and isinstance(fval, list):
                        rec_ids_in_field.update(x for x in fval if is_airtable_record_id(x))
                        break
            # Which table (in our set) contains the most of these IDs?
            best_table: str | None = None
            best_count = 0
            for other_snake in table_snakes:
                if other_snake == t.table_snake:
                    continue
                ids_in_other = record_ids_by_snake.get(other_snake, set())
                count = len(rec_ids_in_field & ids_in_other)
                if count > best_count:
                    best_count = count
                    best_table = other_snake
            col.linked_target_table_snake = best_table if best_count > 0 else None

    # If we have the base schema, use it for exact link targets (overrides inference above)
    if base_schema is not None:
        for t in inferred.values():
            try:
                table_schema = base_schema.table(t.airtable_name)
            except (KeyError, TypeError):
                continue
            for field_schema in getattr(table_schema, "fields", []) or []:
                if getattr(field_schema, "type", None) != "multipleRecordLinks":
                    continue
                options = getattr(field_schema, "options", None)
                linked_table_id = getattr(options, "linked_table_id", None) if options else None
                if not linked_table_id:
                    continue
                try:
                    target_table = base_schema.table(linked_table_id)
                    target_name = getattr(target_table, "name", None)
                    if not target_name:
                        continue
                    target_snake = to_snake_case(target_name)
                    if target_snake not in inferred:
                        continue
                    field_snake = to_snake_case(getattr(field_schema, "name", "") or "")
                    schema_field_name = getattr(field_schema, "name", None)
                    for col in t.columns:
                        if not col.is_linked_record:
                            continue
                        if schema_field_name and col.airtable_field_name == schema_field_name:
                            col.linked_target_table_snake = target_snake
                            break
                        if col.snake_name == field_snake and col.linked_target_table_snake is None:
                            col.linked_target_table_snake = target_snake
                            break
                except (KeyError, TypeError):
                    continue

    return inferred, record_ids_by_snake


# -----------------------------------------------------------------------------
# Phase 2: Creation order and junction tables
# -----------------------------------------------------------------------------

@dataclass
class JunctionTable:
    """A many-to-many junction table for one linked-record field."""
    table_snake: str  # e.g. schools_workshops
    left_table_snake: str  # table that has the field
    right_table_snake: str  # target table of the linked IDs
    field_snake: str  # field name on the left table


def build_junction_tables(inferred: dict[str, InferredTable]) -> list[JunctionTable]:
    """Decide junction tables for each linked-record field."""
    junctions: list[JunctionTable] = []
    for t in inferred.values():
        for col in t.columns:
            if not col.is_linked_record or not col.linked_target_table_snake:
                continue
            right = col.linked_target_table_snake
            left = t.table_snake
            # Junction name: left_right (e.g. schools_workshops)
            name = f"{left}_{right}"
            junctions.append(JunctionTable(
                table_snake=name,
                left_table_snake=left,
                right_table_snake=right,
                field_snake=col.snake_name,
            ))
    return junctions


def creation_order(
    inferred: dict[str, InferredTable],
    junctions: list[JunctionTable],
) -> list[str]:
    """
    Return table names in creation order: base tables first (alphabetical for
    deterministic order when no FKs between them), then junction tables
    (after both referenced tables exist).
    """
    base_tables = sorted(inferred.keys())
    junction_names = [j.table_snake for j in junctions]
    return base_tables + junction_names


# -----------------------------------------------------------------------------
# Phase 3: SQL generation
# -----------------------------------------------------------------------------

def quote_ident(name: str) -> str:
    return f'"{name}"'


def generate_create_table_sql(
    inferred: dict[str, InferredTable],
    junctions: list[JunctionTable],
    schema: str = "public",
) -> str:
    """
    Generate full SQL DDL script.

    Base tables use: id (UUID, primary key), airtable_id (UNIQUE, for sync).
    Relations and junction tables reference id (UUID). When writing a data-pull
    script: match rows by airtable_id, resolve link targets from Airtable rec
    IDs to our UUIDs, then insert/upsert using id in FKs.
    """
    lines: list[str] = []
    schema_ident = quote_ident(schema)

    for table_snake in creation_order(inferred, junctions):
        if table_snake in inferred:
            t = inferred[table_snake]
            lines.append(f"-- Table: {t.airtable_name} (Airtable) → {schema_ident}.{quote_ident(table_snake)}")
            lines.append(f"CREATE TABLE IF NOT EXISTS {schema_ident}.{quote_ident(table_snake)} (")
            # UUID primary key (controlled by us); airtable_id for sync lookups; then columns
            col_defs = [
                "  id UUID PRIMARY KEY DEFAULT gen_random_uuid()",
                "  airtable_id TEXT UNIQUE NOT NULL",
                "  created_time TIMESTAMPTZ",
            ]
            for col in t.columns:
                if col.is_linked_record:
                    continue
                col_defs.append(f"  {quote_ident(col.snake_name)} {col.pg_type}")
            lines.append(",\n".join(col_defs))
            lines.append(");")
            lines.append("")
        else:
            # Junction table: FKs reference id (UUID), not airtable_id (sync script resolves via airtable_id)
            j = next(j for j in junctions if j.table_snake == table_snake)
            left_id_col = f"{j.left_table_snake}_id"
            right_id_col = f"{j.right_table_snake}_id"
            lines.append(f"-- Junction: {j.left_table_snake} ↔ {j.right_table_snake} (from field {j.field_snake})")
            lines.append(f"CREATE TABLE IF NOT EXISTS {schema_ident}.{quote_ident(j.table_snake)} (")
            lines.append("  id SERIAL PRIMARY KEY,")
            lines.append(f"  {quote_ident(left_id_col)} UUID NOT NULL")
            lines.append(f"    REFERENCES {schema_ident}.{quote_ident(j.left_table_snake)}(id) ON DELETE CASCADE,")
            lines.append(f"  {quote_ident(right_id_col)} UUID NOT NULL")
            lines.append(f"    REFERENCES {schema_ident}.{quote_ident(j.right_table_snake)}(id) ON DELETE CASCADE,")
            lines.append(f"  UNIQUE ({quote_ident(left_id_col)}, {quote_ident(right_id_col)})")
            lines.append(");")
            lines.append("")

    return "\n".join(lines)


# -----------------------------------------------------------------------------
# Execution: run generated SQL against Postgres (optional)
# -----------------------------------------------------------------------------

def run_sql(sql: str, database_url: str) -> None:
    """Execute SQL using psycopg2 if available."""
    try:
        import psycopg2
    except ImportError:
        raise RuntimeError("Applying SQL requires psycopg2. Install with: pip install psycopg2-binary")
    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
    finally:
        conn.close()


# -----------------------------------------------------------------------------
# Standalone and FastAPI background task entrypoints
# -----------------------------------------------------------------------------

def run_inference_and_generate(
    table_names: list[str] | None,
    api_key: str,
    base_id: str,
    schema: str = "public",
    all_tables: bool = False,
) -> tuple[dict[str, InferredTable], list[JunctionTable], str]:
    """
    Single entrypoint: fetch from Airtable, infer schema, return inferred state
    and generated SQL. Can be called from a FastAPI background task or CLI.

    If all_tables is True, table_names is ignored and all tables in the base
    are fetched (via the base schema API). Related tables are then included
    automatically, and link targets are resolved from the schema.

    Returns (inferred_tables, junction_tables, sql_string).
    """
    if Api is None:
        raise RuntimeError("pyairtable is required. Install with: pip install pyairtable")
    api = Api(api_key)
    base = api.base(base_id)

    if all_tables:
        table_names = get_all_table_names(api, base_id)
        if not table_names:
            raise RuntimeError("Base has no tables or schema could not be loaded")

    if not table_names:
        raise ValueError("No tables to process. Use --tables or --all-tables.")

    records_by_table = fetch_all_records(api, base_id, table_names)
    base_schema = base.schema()
    inferred, _ = infer_schema(table_names, records_by_table, base_schema=base_schema)
    junctions = build_junction_tables(inferred)
    sql = generate_create_table_sql(inferred, junctions, schema=schema)
    return inferred, junctions, sql


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Infer Postgres schema from Airtable tables and generate DDL",
    )
    parser.add_argument(
        "--tables",
        nargs="*",
        default=None,
        help="Airtable table names (e.g. Schools Workshops). Omit when using --all-tables.",
    )
    parser.add_argument(
        "--all-tables",
        action="store_true",
        help="Fetch all tables in the base (uses base schema API). Includes related tables and resolves links from schema.",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=None,
        help="Write SQL to this file instead of stdout",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply generated SQL (requires DATABASE_URL and psycopg2)",
    )
    parser.add_argument(
        "--schema",
        default="public",
        help="Postgres schema name (default: public)",
    )
    args = parser.parse_args()

    from src.config import settings

    if not settings.airtable_api_key or not settings.airtable_base_id:
        print("Error: AIRTABLE_API_KEY and AIRTABLE_BASE_ID must be set in .env", file=sys.stderr)
        return 1

    if not args.all_tables and not args.tables:
        print("Error: specify --tables Table1 Table2 ... or --all-tables", file=sys.stderr)
        return 1

    try:
        inferred, junctions, sql = run_inference_and_generate(
            args.tables if not args.all_tables else None,
            settings.airtable_api_key,
            settings.airtable_base_id,
            schema=args.schema,
            all_tables=args.all_tables,
        )
    except Exception as e:
        print(f"Error during inference: {e}", file=sys.stderr)
        raise

    if args.output:
        args.output.write_text(sql, encoding="utf-8")
        print(f"Wrote SQL to {args.output}", file=sys.stderr)
    else:
        print(sql)

    if args.apply:
        if not settings.database_url:
            print("Error: DATABASE_URL must be set to use --apply", file=sys.stderr)
            return 1
        try:
            run_sql(sql, settings.database_url)
            print("SQL applied successfully.", file=sys.stderr)
        except Exception as e:
            print(f"Error applying SQL: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
