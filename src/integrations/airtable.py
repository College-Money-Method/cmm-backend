"""Airtable integration — read-only record fetching for sync operations."""
from __future__ import annotations

from pyairtable import Api

from src.config import settings

WEBINARS_TABLE = "Junction Table School Workshop"
SCHOOLS_TABLE = "Schools"
CONTACTS_TABLE = "Contacts"
COHORTS_TABLE = "Cohort"


def get_webinar_records() -> list[dict]:
    """Fetch all records from the Airtable webinars table (auto-paginates)."""
    api = Api(settings.airtable_api_key)
    return api.table(settings.airtable_base_id, WEBINARS_TABLE).all()


def get_schools_records() -> list[dict]:
    """Fetch all records from the Airtable Schools table (auto-paginates)."""
    api = Api(settings.airtable_api_key)
    return api.table(settings.airtable_base_id, SCHOOLS_TABLE).all()


def get_contacts_records() -> list[dict]:
    """Fetch all records from the Airtable Contacts table (auto-paginates)."""
    api = Api(settings.airtable_api_key)
    return api.table(settings.airtable_base_id, CONTACTS_TABLE).all()


def get_cohorts_records() -> list[dict]:
    """Fetch all records from the Airtable Cohorts table (auto-paginates)."""
    api = Api(settings.airtable_api_key)
    return api.table(settings.airtable_base_id, COHORTS_TABLE).all()
