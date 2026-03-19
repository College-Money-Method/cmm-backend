# cmm-backend

FastAPI backend for CMM: Supabase (PostgreSQL) and AWS S3.

## Local development

- **Database**: Use local Supabase via the **`supabase/`** folder. From project root run `supabase start`, then set `SUPABASE_URL` (e.g. `http://127.0.0.1:54321`) and `SUPABASE_KEY` (from `supabase status`) in `.env`.
- **Testing DB**: Config uses `SUPABASE_DB_NAME=cmm_dev` by default (see `src/config.py`). See `supabase/README.md` for creating a `cmm_dev` DB locally.
- Copy `.env.example` to `.env` and fill in values.

## Run

```bash
uv run python -m src.main
# or
uv run uvicorn src.main:app --reload --port 8000
```

`GET /health` returns status and non-secret config (supabase_url, supabase_db_name, s3_bucket).
