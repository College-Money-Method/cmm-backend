"""Application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import all models FIRST so SQLAlchemy mapper can resolve cross-module
# relationships before any router (which imports models) is loaded.
import src.assets.models  # noqa: F401
import src.auth.models  # noqa: F401
import src.calendar.models  # noqa: F401
import src.cycles.models  # noqa: F401
import src.meetings.models  # noqa: F401
import src.sales.models  # noqa: F401
import src.schools.models  # noqa: F401
import src.settings.models  # noqa: F401
import src.workshops.models  # noqa: F401

from src.auth.router import router as auth_router
from src.config import settings
from src.cycles.router import router as cohorts_router
from src.db import get_supabase
from src.schools.router import router as schools_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: ensure Supabase client is created. Shutdown: nothing."""
    get_supabase()
    yield


app = FastAPI(
    title="CMM Backend",
    description="CMM API with Supabase (PostgreSQL) and AWS S3",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_url],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(schools_router)
app.include_router(cohorts_router)


@app.get("/health")
def health():
    """Health check: reports config (no secrets)."""
    return {
        "status": "ok",
        "environment": settings.environment,
        "supabase_url": settings.supabase_url,
        "supabase_db_name": settings.supabase_db_name,
        "s3_bucket": settings.s3_bucket_name or "(not set)",
    }


def main():
    """Run the app (e.g. python -m src.main)."""
    import uvicorn
    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.debug,
    )


if __name__ == "__main__":
    main()
