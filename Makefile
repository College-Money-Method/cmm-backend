.DEFAULT_GOAL := help
.PHONY: help dev build run stop logs shell \
        migrate upgrade downgrade revision history current \
        seed-admins import-assets migrate-wp \
        lint format typecheck

# ── Help ──────────────────────────────────────────────────────────────────────

help:
	@echo ""
	@echo "  CMM Backend"
	@echo ""
	@echo "  Dev"
	@echo "    make dev              Start API with hot reload (uvicorn)"
	@echo "    make install          Install all dependencies via uv"
	@echo "    make install-scripts  Install + scripts extras (pandas etc.)"
	@echo ""
	@echo "  Docker"
	@echo "    make build            Build the Docker image"
	@echo "    make run              Start containers (docker-compose up -d)"
	@echo "    make stop             Stop containers"
	@echo "    make logs             Tail container logs"
	@echo "    make shell            Open a shell inside the running container"
	@echo ""
	@echo "  Alembic"
	@echo "    make upgrade          Apply all pending migrations (alembic upgrade heads)"
	@echo "    make downgrade        Roll back one migration (alembic downgrade -1)"
	@echo "    make revision MSG=... Auto-generate a new migration"
	@echo "    make history          Show migration history"
	@echo "    make current          Show current DB revision"
	@echo ""
	@echo "  Scripts"
	@echo "    make seed-admins      Seed super admin accounts"
	@echo "    make import-assets    Import content assets from Airtable"
	@echo "    make migrate-wp       Migrate WordPress content (set WP_DOMAIN=...)"
	@echo "    make migrate-wp-dry   Dry run WordPress migration"
	@echo ""

# ── Dev ───────────────────────────────────────────────────────────────────────
PORT ?= 8000

dev:
	uv run uvicorn src.main:app --reload --host 0.0.0.0 --port $(PORT) --log-level debug

install:
	uv sync --frozen

install-scripts:
	uv sync --frozen --extra scripts

# ── Docker ────────────────────────────────────────────────────────────────────

build:
	docker compose build

run:
	docker compose up -d

stop:
	docker compose down

logs:
	docker compose logs -f api

shell:
	docker compose exec api /bin/bash

# ── Alembic ───────────────────────────────────────────────────────────────────

upgrade:
	uv run alembic upgrade heads

downgrade:
	uv run alembic downgrade -1

# Usage: make revision MSG="add something"
revision:
	@if [ -z "$(MSG)" ]; then echo "Usage: make revision MSG=\"describe the change\"" && exit 1; fi
	uv run alembic revision --autogenerate -m "$(MSG)"

history:
	uv run alembic history --verbose

current:
	uv run alembic current

# ── Scripts ───────────────────────────────────────────────────────────────────

seed-admins:
	uv run python scripts/seed_super_admins.py

import-assets:
	uv run python scripts/import_content_assets.py

# Usage: make migrate-wp WP_DOMAIN=https://yoursite.com
migrate-wp:
	@if [ -z "$(WP_DOMAIN)" ]; then echo "Usage: make migrate-wp WP_DOMAIN=https://yoursite.com" && exit 1; fi
	uv run python scripts/migrate_wp_content.py --wp-domain $(WP_DOMAIN)

migrate-wp-dry:
	@if [ -z "$(WP_DOMAIN)" ]; then echo "Usage: make migrate-wp-dry WP_DOMAIN=https://yoursite.com" && exit 1; fi
	uv run python scripts/migrate_wp_content.py --wp-domain $(WP_DOMAIN) --dry-run
