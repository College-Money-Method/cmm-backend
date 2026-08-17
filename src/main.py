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
import src.storage.models  # noqa: F401
import src.pages.models  # noqa: F401
import src.app_config.models  # noqa: F401
import src.surveys.models  # noqa: F401
import src.communications.models  # noqa: F401
import src.communications.schedule_model  # noqa: F401
import src.communications.template_default_date_model  # noqa: F401
import src.content.translation_models  # noqa: F401
import src.emails.models  # noqa: F401
import src.emails.broadcast_models  # noqa: F401
import src.emails.automation_models  # noqa: F401
import src.emails.email_template_models  # noqa: F401
import src.emails.automation_ledger_models  # noqa: F401

from src.auth.router import router as auth_router
from src.config import settings
from src.content.router import router as content_router
from src.content.submissions_router import router as submissions_router
from src.search.router import router as search_router
from src.cycles.router import router as cohorts_router
from src.db import get_supabase
from src.schools.router import router as schools_router
from src.workshops.router import router as workshops_router
from src.guest_contacts.router import router as guest_contacts_router
from src.storage.router import router as storage_router
from src.pages.router import router as pages_router
from src.app_config.router import router as app_config_router
from src.analytics.router import router as analytics_router
from src.analytics.admin_router import router as analytics_admin_router
from src.communications.router import router as communications_router
from src.surveys.router import router as surveys_router
from src.surveys.config_router import router as survey_configs_router
from src.zoom.webhook_router import router as zoom_webhook_router
from src.content.translation_router import router as translation_router
from src.content.video_cc_router import router as video_cc_router
from src.emails.webhook_router import router as emails_webhook_router
from src.emails.unsubscribe_router import router as emails_unsubscribe_router
from src.emails.broadcast_router import router as emails_broadcast_router
from src.emails.automation_router import router as emails_automation_router
from src.emails.template_router import router as emails_template_router
from src.emails.preview_router import router as emails_preview_router
from src.emails.scheduler import init_scheduler, shutdown_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: ensure Supabase client is created, start the in-process
    email-automation scheduler. Shutdown: stop the scheduler."""
    get_supabase()
    init_scheduler(app)
    yield
    shutdown_scheduler()


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
app.include_router(submissions_router)
app.include_router(workshops_router)
app.include_router(guest_contacts_router)
app.include_router(storage_router)
app.include_router(search_router)
app.include_router(pages_router)
app.include_router(app_config_router)
app.include_router(analytics_router)
app.include_router(analytics_admin_router)
app.include_router(communications_router)
app.include_router(surveys_router)
app.include_router(survey_configs_router)
app.include_router(zoom_webhook_router)
app.include_router(translation_router)
app.include_router(video_cc_router)
app.include_router(emails_webhook_router)
app.include_router(emails_unsubscribe_router)
app.include_router(emails_broadcast_router)
app.include_router(emails_automation_router)
app.include_router(emails_template_router)
app.include_router(emails_preview_router)


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
