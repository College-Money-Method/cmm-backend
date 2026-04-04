# ── Base image ────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS base

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# ── Dependencies ──────────────────────────────────────────────────────────────
FROM base AS deps

# Copy lockfile and project metadata first for layer caching
COPY pyproject.toml ./

# Install only third-party production dependencies in this cacheable layer.
# The app source is copied in the runtime stage and run directly from /app.
RUN uv sync --frozen --no-cache --no-dev --no-install-project

# ── Runtime ───────────────────────────────────────────────────────────────────
FROM base AS runtime

WORKDIR /app

# Copy installed packages from deps stage
COPY --from=deps /app/.venv /app/.venv

# Copy application source
COPY . .

# Use the venv created by uv
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
