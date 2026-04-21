"""Application configuration from environment."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings loaded from environment (e.g. .env)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Supabase (local or hosted)
    supabase_url: str = "http://127.0.0.1:54321"
    supabase_key: str = ""
    # Service role key (bypasses RLS); use for scripts/imports only, never expose to frontend
    supabase_service_role_key: str = ""
    # Database name for local dev / testing (e.g. Postgres database or schema identifier)
    supabase_db_name: str = "cmm_dev"

    # Airtable (for schema inference / sync scripts)
    airtable_api_key: str = ""
    airtable_base_id: str = ""
    airtable_asset_base_id: str = ""
    # Optional: direct Postgres URL for running DDL (from Supabase Dashboard -> Database -> Connection string).
    database_url: str = ""

    # AWS S3
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    s3_bucket_name: str = ""

    # WordPress (for media migration script)
    wordpress_application_password: str = ""

    # Zoom (Server-to-Server OAuth — for webinar registrations)
    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: str = ""

    # App
    log_level: str = "DEBUG"
    debug: bool = False
    environment: str = "development"
    frontend_url: str = "http://localhost:5173"


settings = Settings()
