# Scripts

One-off operational scripts for data import, migration, backfills, seeding, and
release ops. Organized by workflow. **`input/` and `output/` stay at this root**
(scripts read/write there regardless of their category folder).

## Conventions

- **Always run from the repo root** (`cmm-backend/`), not from inside `scripts/`.
  Every script resolves the repo root relative to its own location and expects the
  current working directory to be the root so `src.*` and `scripts.*` imports resolve.
- **Python**: run with `uv run python scripts/<category>/<name>.py`, or as a module
  `python -m scripts.<category>.<name>` where noted (dotted path, no `.py`).
- **Shell**: `bash scripts/db_ops/<name>.sh`.
- **Env files**: most DB scripts read `.env.dev` / `.env.prod` (or `.env`) at the repo
  root for `DATABASE_URL` and Supabase/AWS credentials.
- **Dry-run first**: scripts that write to the DB default to (or support) `--dry-run`
  / `--apply`. Preview before committing.
- `--extra scripts` (e.g. `uv run --extra scripts python ...`) installs optional deps
  (Firecrawl, etc.) needed by the crawl/seed scripts.

## Layout

```
scripts/
├── input/               # source data (CSV/XLSX/HTML) — consumed by ingest/import
├── output/              # generated artifacts (crawl md, revision exports)
├── airtable/            # pull data & schema from Airtable
├── import_ingest/       # import/ingest content into the DB (CSV, Google Docs, crawl)
├── migrate_s3/          # move images/logos/art/thumbnails to S3
├── migrate_wordpress/   # migrate WordPress content & media
├── migrate_data/        # reshape existing DB data (denormalized → item tables)
├── backfill/            # populate missing columns/rows on existing records
├── seed/                # create baseline accounts/pages
├── content_fix/         # one-off content corrections & dedupe
├── export/              # export DB content to files
├── db_ops/              # DB/release/infra ops (shell + storage sync)
└── debug/               # read-only inspection helpers
```

---

## airtable/ — Airtable extraction

| Script | Description | Run |
|--------|-------------|-----|
| `airtable_schema_to_postgres.py` | Infer Postgres schema from Airtable tables, emit SQL migration. Run first in the schema→reset→pull flow. | `uv run python scripts/airtable/airtable_schema_to_postgres.py --all-tables -o supabase/migrations/<ts>_airtable_schema.sql` |
| `airtable_pull_data.py` | Pull Airtable records into the matching Postgres tables (schema must already exist). | `uv run python scripts/airtable/airtable_pull_data.py --all-tables [--dry-run]` |
| `airtable_export_csv.py` | Export Airtable tables to CSV files. | `uv run python scripts/airtable/airtable_export_csv.py [--all-tables] [--output-dir ./csv_exports]` |
| `analyze_airtable_contacts_missing_from_supabase.py` | Report Airtable contacts with no matching Supabase auth user. | `uv run python scripts/airtable/analyze_airtable_contacts_missing_from_supabase.py [--csv-out <path>]` |

## import_ingest/ — Import & ingest content

| Script | Description | Run |
|--------|-------------|-----|
| `import_csv_data.py` | Import Airtable CSV exports (from `airtable_csv_exports/`) into Postgres. | `uv run python scripts/import_ingest/import_csv_data.py [--table cycles] [--dry-run]` |
| `import_content_assets.py` | Import content assets + asset types into the DB. | `uv run python scripts/import_ingest/import_content_assets.py [--dry-run] [--reset] [--table asset_types]` |
| `import_topics_from_google_docs.py` | Batch-import Google Docs HTML exports into the `topics` table (AI-assisted parsing). | `uv run python scripts/import_ingest/import_topics_from_google_docs.py --input <topics.csv> --provider openai [--dry-run] [--create-missing]` |
| `import_workshops_from_google_docs.py` | Batch-import Google Docs workshop pages into `workshops`. | `uv run python scripts/import_ingest/import_workshops_from_google_docs.py --input scripts/input/workshops.csv --provider openai [--dry-run] [--create-missing] [--overwrite]` |
| `ingest_resource_csv.py` | Ingest CMM Resource Center assets CSV (`input/resource_ingest/…`) → content_assets + topic/workshop links. | `uv run python scripts/import_ingest/ingest_resource_csv.py [--dry-run]` |
| `crawl_marketing_site.py` | Crawl the marketing site (Firecrawl); saves one `.md` per page to `output/crawl/`. | `uv run --extra scripts python scripts/import_ingest/crawl_marketing_site.py [--limit 50] [--url <url>]` |
| `link_resources_to_storage.py` | Match resource assets to existing storage files and link them (resource pipeline step 2). | `uv run python scripts/import_ingest/link_resources_to_storage.py [--env-file .env.dev] [--dry-run]` |

## migrate_s3/ — Media → S3

| Script | Description | Run |
|--------|-------------|-----|
| `migrate_images_to_s3.py` | Migrate workshop art & content-asset images from Airtable to S3. | `uv run python scripts/migrate_s3/migrate_images_to_s3.py [--dry-run] [--only workshops\|content]` |
| `migrate_logos_to_s3.py` | Fetch fresh school logos from Airtable, thumbnail them, store in S3. | `uv run python scripts/migrate_s3/migrate_logos_to_s3.py [--dry-run] [--school "Name"]` |
| `upload-topic-art-to-s3.py` | Upload topic artwork SVGs + grade hero banners to S3 and link in DB. | `uv run python scripts/migrate_s3/upload-topic-art-to-s3.py [--apply] [--svg-dir <dir>]` |
| `upload-thumbnail-svgs-to-s3.py` | Upload rendered thumbnail SVGs to `portal/assets/thumbnails/` on S3. | `python scripts/migrate_s3/upload-thumbnail-svgs-to-s3.py [--svg-dir <dir>]` |
| `attach-thumbnails-to-db.py` | Match uploaded thumbnail SVGs to DB records and optionally write links. | `uv run python scripts/migrate_s3/attach-thumbnails-to-db.py [--apply] [--list-asset-types]` |
| `upload_to_s3.py` | Upload one or more image URLs to S3, print permanent URLs. | `python scripts/migrate_s3/upload_to_s3.py <url> [<url> ...]` |

## migrate_wordpress/ — WordPress migration

| Script | Description | Run |
|--------|-------------|-----|
| `migrate_wp_content.py` | Import WordPress posts (name/content/description) into `content_assets`. | `uv run python scripts/migrate_wordpress/migrate_wp_content.py --wp-domain <url> [--dry-run] [--overwrite]` |
| `migrate_wp_assets_to_tiptap.py` | Convert WP-linked content assets to Tiptap JSON (fetch, parse, save content + audit cols). | `uv run python scripts/migrate_wordpress/migrate_wp_assets_to_tiptap.py --wp-domain <url> [--dry-run] [--overwrite] [--asset-id <uuid>]` |
| `migrate_wordpress_media.py` | Download WordPress media to S3 and rewrite DB references. | `uv run python scripts/migrate_wordpress/migrate_wordpress_media.py [--dry-run] [--skip-download]` |
| `migrate_wp_content_files_to_s3.py` | Repoint `content_assets.link` from wp-content uploads to S3. | `uv run python scripts/migrate_wordpress/migrate_wp_content_files_to_s3.py [--dry-run]` |

## migrate_data/ — DB data reshaping

| Script | Description | Run |
|--------|-------------|-----|
| `migrate_key_actions_to_key_action_items.py` | Split workshop `key_actions` text into `key_action_items` rows. | `python -m scripts.migrate_data.migrate_key_actions_to_key_action_items [--dry-run] [--overwrite]` |
| `migrate_summary_to_summary_items.py` | Split topic `summary` text into `summary_items` rows. | `python -m scripts.migrate_data.migrate_summary_to_summary_items [--dry-run] [--overwrite]` |

## backfill/ — Backfill missing data

| Script | Description | Run |
|--------|-------------|-----|
| `backfill-asset-audience-from-airtable.py` | Set content-asset audience from Airtable. | `uv run python scripts/backfill/backfill-asset-audience-from-airtable.py [--dry-run]` |
| `backfill_counselor_default_passwords.py` | Set default passwords for counselor/director accounts that have none. | `uv run python scripts/backfill/backfill_counselor_default_passwords.py [--dry-run]` |
| `backfill_hub_permissions.py` | Set `hub_permission` values from Airtable roles. | `uv run python scripts/backfill/backfill_hub_permissions.py [--dry-run]` |
| `backfill_read_watch_times.py` | Compute read/watch times for content (NULL rows by default). | `python -m scripts.backfill.backfill_read_watch_times [--all] [--dry-run]` |
| `backfill_search_text.py` | Populate the search-text column across searchable records. | `python -m scripts.backfill.backfill_search_text` |

## seed/ — Seed baseline records

| Script | Description | Run |
|--------|-------------|-----|
| `seed_counselors_from_contacts.py` | Create counselor accounts from school contacts. | `uv run python scripts/seed/seed_counselors_from_contacts.py [--dry-run]` |
| `seed_super_admins.py` | Seed `super_admin` role records for CMM team members. | `uv run python scripts/seed/seed_super_admins.py [--dry-run]` |
| `seed_pages_from_crawl.py` | Seed pages from crawl output; uploads WP assets to S3. | `uv run --extra scripts python scripts/seed/seed_pages_from_crawl.py [--dry-run] [--force] [--env dev]` |

## content_fix/ — Content corrections

| Script | Description | Run |
|--------|-------------|-----|
| `fix_content_colors.py` | Normalize content colors across content tables. | `uv run python -m scripts.content_fix.fix_content_colors [--apply] [--table content_assets]` |
| `fix_content_urls.py` | Rewrite stale `collegemoneymethod.com/wp-content/uploads/...` URLs. | `uv run python -m scripts.content_fix.fix_content_urls [--apply] [--table content_assets]` |
| `dedupe_schools_and_counselors.py` | Merge duplicate schools (and their counselors/contacts) into one canonical row (idempotent, single transaction). | `uv run python scripts/content_fix/dedupe_schools_and_counselors.py [--apply]` |

## export/ — Export DB → files

| Script | Description | Run |
|--------|-------------|-----|
| `export_topics_content_revisions.py` | Export topic content revisions to `output/topics_revisions/`. | `uv run python -m scripts.export.export_topics_content_revisions [--database-url <url>]` |
| `generate_topic_illustration_prompts.py` | Build one standalone CMM illustration prompt per topic (title → SUBJECT, description + key takeaways → CONCEPT) from `input/cmm-topic-illustration-prompt-template.md` → `output/topic_illustration_prompts/`. Reads **PROD** (`.env.prod`), read-only. **Calls OpenAI once per topic** for per-topic art direction (scene, props, labels) — `--provider none` skips it and writes placeholders. | `uv run python -m scripts.export.generate_topic_illustration_prompts [--dry-run] [--slug <slug>] [--status all] [--provider none] [--scene "…"] [--skip-existing]` |

## db_ops/ — Database / release / infra ops

| Script | Description | Run |
|--------|-------------|-----|
| `copy-dev-to-prod-db.sh` | Fully replace prod `public` schema with a copy of dev (DROP + recreate). One-shot launch step; Supabase-managed schemas untouched. | `bash scripts/db_ops/copy-dev-to-prod-db.sh` |
| `sync-dev-auth-users-to-prod.sh` | Additively copy dev `auth.users` + `auth.identities` into prod (only new emails; keeps dev UUIDs). Idempotent. | `bash scripts/db_ops/sync-dev-auth-users-to-prod.sh` |
| `release-to-production.sh` | Merge `main` → `production` for frontend + backend over SSH in a throwaway worktree; pushes with a `release: DD/MM/YYYY` commit. | `bash scripts/db_ops/release-to-production.sh` |
| `upload-ssm-dev.sh` | Upload secrets from `.env.<env>` to AWS SSM Parameter Store. | `bash scripts/db_ops/upload-ssm-dev.sh <dev\|prod>` |
| `sync_storage_files_between_dbs.py` | Copy `storage.files` rows between DBs (s3_url values stay valid). | `uv run python scripts/db_ops/sync_storage_files_between_dbs.py --source-env .env --target-env .env.dev [--dry-run]` |

## debug/ — Inspection

| Script | Description | Run |
|--------|-------------|-----|
| `check_callout_attrs.py` | Inspect callout-node attrs for a specific content asset (read-only). | `python -m scripts.debug.check_callout_attrs` |
