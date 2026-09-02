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
    # object at send time, unlike interactive routes).
    #
    # Defaults to the live site rather than "" on purpose: an empty origin used
    # to yield *relative* hrefs, which mail clients absolutize against nothing —
    # recipients got "http:///school/..." and a Google redirect warning instead
    # of the workshop page. A wrong-but-absolute default degrades far more
    # gracefully than a hostless link, so links stay clickable even when the
    # deployment forgets to set APP_PUBLIC_URL (which prod did — see
    # `email_origin()` in src/emails/school_links.py).
    app_public_url: str = "https://collegemoneymethod.com"
    ses_from_email: str = "noreply@collegemoneymethod.com"
    # Display name paired with ses_from_email when a send picks no sender of its own.
    ses_from_name: str = "College Money Method"
    # Presets the broadcast/automation compose UI offers, comma-separated
    # "Name <email>" entries. Suggestions only — ses_allowed_sender_domains is
    # the actual guard (see src/emails/sender.py).
    ses_sender_options: str = (
        "College Money Method <noreply@collegemoneymethod.com>,"
        "CMM Newsflash <newsflash@collegemoneymethod.com>,"
        "Paul Martin <paul.martin@collegemoneymethod.com>"
    )
    # Domains the app may send as. An address outside these is rejected at save
    # time rather than failing per-recipient at SES (unverified identity).
    ses_allowed_sender_domains: str = "collegemoneymethod.com"
    # Comma-separated From addresses whose mail carries NO unsubscribe mechanism
    # — neither the visible footer link nor the List-Unsubscribe header (see
    # src/emails/sender.py::sender_omits_unsubscribe).
    #
    # This is a deliberate, narrow exception, not a default to widen casually.
    # It exists for one-to-one style mail to a small, warm, already-opted-in
    # audience (Paul's counselor contacts), where the personal read matters more
    # than the footer. Adding a sender that mails a broad or cold list would
    # push opt-outs into "Report Spam" instead, and complaint rate is scored
    # against the whole sending domain — degrading delivery for ALL CMM mail,
    # automations and transactional included. It also forfeits the CAN-SPAM
    # opt-out mechanism, which commercial email is required to provide.
    ses_no_unsubscribe_senders: str = "paul.martin@collegemoneymethod.com"

    # IANA zone that workshop {{date}}/{{time}} merge tags render in when the
    # admin has not set one in Global Settings. Workshop datetimes are stored
    # in UTC, which is a day ahead for any US evening event — see
    # src/schools/display_timezone.py. Keep in step with DEFAULT_DISPLAY_TIMEZONE
    # in cmm-frontend's app/lib/us-timezones.ts, or the Hub preview of an email
    # will disagree with the email that actually goes out.
    workshop_display_timezone: str = "America/New_York"
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
