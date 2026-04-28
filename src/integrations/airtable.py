"""Airtable integration — read-only record fetching for sync operations."""
from __future__ import annotations

from pyairtable import Api

from src.config import settings

WEBINARS_TABLE = "Junction Table School Workshop"


def get_webinar_records() -> list[dict]:
    """Fetch all records from the Airtable webinars table (auto-paginates)."""
    api = Api(settings.airtable_api_key)
    return api.table(settings.airtable_base_id, WEBINARS_TABLE).all()
