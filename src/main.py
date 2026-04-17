"""Application entry point."""

import logging
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Import all models FIRST so SQLAlchemy mapper can resolve cross-module
# relationships before any router (which imports models) is loaded.
import src.assets.models  # noqa: F401
import src.auth.models  # noqa: F401
import src.calendar.models  # noqa: F401
import src.content.models  # noqa: F401
import src.cycles.models  # noqa: F401
import src.meetings.models  # noqa: F401
import src.sales.models  # noqa: F401
import src.schools.models  # noqa: F401
import src.settings.models  # noqa: F401
import src.workshops.models  # noqa: F401
import src.guest_contacts.models  # noqa: F401

from src.auth.router import router as auth_router
from src.config import settings
from src.content.router import router as content_router
from src.search.router import router as search_router
from src.cycles.router import router as cohorts_router
from src.db import get_supabase
from src.schools.router import router as schools_router
from src.workshops.router import router as workshops_router
from src.guest_contacts.router import router as guest_contacts_router
from src.storage.router import router as storage_router


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
    allow_origin_regex=r"https://(.*\.)?collegemoneymethod\.com",
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(schools_router)
app.include_router(cohorts_router)
app.include_router(content_router)
app.include_router(workshops_router)
app.include_router(guest_contacts_router)
app.include_router(storage_router)
app.include_router(search_router)


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
