"""Application configuration from environment."""

from pydantic_settings import BaseSettings, SettingsConfigDict

# Supported translation locales: code → full language name used in the prompt.
# Not a secret — safe to define at module level.
SUPPORTED_LOCALES: dict[str, str] = {
    "es": "Spanish",
    "zh": "Chinese (Simplified)",
    "zh-Hant": "Chinese (Traditional)",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "vi": "Vietnamese",
    "ja": "Japanese",
    "ko": "Korean",
    "hi": "Hindi",
    "ar": "Arabic",
}


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

    # Public CDN base URL for serving S3 assets (e.g.
    # "https://cdn.next.collegemoneymethod.com"). When set, asset URLs are
    # rewritten from the raw S3 host to this CDN host at read time. Empty =
    # fall back to direct S3 URLs (kill-switch). Swapping this value re-points
    # all existing asset URLs with no data migration.
    cdn_base_url: str = ""

    # AWS Bedrock — translation pipeline
    # Region for Bedrock API calls; defaults to the same region as S3/general AWS.
    bedrock_region: str = "us-east-1"
    # Claude Haiku via the classic AnthropicBedrock (InvokeModel) client.
    # Must be a cross-region INFERENCE-PROFILE id — Haiku 4.5 rejects bare
    # on-demand model ids. "us." keeps routing within US regions; "global."
    # (global.anthropic.claude-haiku-4-5-20251001-v1:0) routes worldwide.
    # Override via env var BEDROCK_HAIKU_MODEL_ID.
    # IAM: needs bedrock:InvokeModel(+WithResponseStream) — AmazonBedrockFullAccess covers it.
    bedrock_haiku_model_id: str = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    # Haiku 4.5 pricing (USD per 1M tokens) — used to compute cost_usd on each
    # recorded translation invocation. Override if AWS pricing changes.
    bedrock_haiku_input_usd_per_mtok: float = 1.0
    bedrock_haiku_output_usd_per_mtok: float = 5.0

    # WordPress (for media migration script)
    wordpress_application_password: str = ""

    # Zoom (Server-to-Server OAuth — for webinar registrations)
    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: str = ""
    # Zoom webhook secret token (from Marketplace app → Event Subscriptions)
    zoom_webhook_secret_token: str = ""

    # Vimeo (personal access token — scopes: public private edit upload delete).
    # 'upload' is required because Vimeo treats writing a text-track FILE as an
    # upload; 'delete' is required to replace an existing track for a language.
    # See src.integrations.vimeo.REQUIRED_SCOPES.
    # Used by the Video CC utility to create/replace text tracks on videos.
    # Permissions are evaluated against the token owner's team role, not video
    # ownership, so a team member with edit rights can manage another user's videos.
    vimeo_access_token: str = ""

    # PostHog (analytics queries — server-side only)
    posthog_api_key: str = ""
    posthog_project_id: str = ""
    # PostHog project token (phc_...) — same public token the frontend uses;
    # required for server-side event capture ($groupidentify)
    posthog_project_token: str = ""

    # App
    log_level: str = "DEBUG"
    debug: bool = False
    environment: str = "development"
    frontend_url: str = "http://localhost:5173"

    # SES (automation email sender)
    # Shared Configuration Set (bounce/complaint events -> SNS -> webhook_router).
    ses_configuration_set_name: str = ""
    # ARN of the SNS topic that fronts the SES event destination. The webhook
    # rejects any SNS message whose TopicArn != this, so a merely AWS-signed
    # message published from a different (e.g. attacker-owned) topic cannot
    # confirm a subscription or write suppression/event rows. Empty disables the
    # check (dev/test, where no SNS subscription targets the webhook at all).
    ses_sns_topic_arn: str = ""
    # Absolute origin used to build school-scoped links in emails (no `request`
    # object at send time, unlike interactive routes) e.g. "https://next.collegemoneymethod.com".
    app_public_url: str = ""
    ses_from_email: str = "noreply@collegemoneymethod.com"
    # NOTE: outbound email is always attempted. The only safety guard is the
    # runtime "email sandbox mode" flag stored on the global app config
    # (AppConfig.email_sandbox_mode) — see src/emails/ses_client.py. When on,
    # only recipients on the team domain are sent; everyone else is logged, not
    # sent. Typically on in local/dev, off in production.
    # Signing secret for the public CAN-SPAM unsubscribe link (src/emails/unsubscribe.py).
    # Falls back to the Supabase service role key when unset so dev/test need no new
    # env var; prod should still set a dedicated key to keep the two secrets isolated.
    unsubscribe_secret_key: str = ""

    # Airtable sync — offboarding safety. When False, the counselor revoke pass
    # runs in log-only mode (reports what WOULD be revoked without acting). Flip
    # to True after reviewing the first-deploy logs to enable live revocation.
    sync_enable_revoke: bool = False
    # Skip contact deactivation if more than this fraction of known Airtable-linked
    # contacts are missing from a pull (guards against partial/failed Airtable fetch).
    sync_deactivation_max_missing_fraction: float = 0.1


settings = Settings()
