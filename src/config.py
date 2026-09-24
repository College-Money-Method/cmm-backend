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
    # Sonnet, used only where judgement over a long text is worth ~3x the price:
    # picking trailer clips from a webinar transcript. Same inference-profile
    # rule as Haiku. Override via env var BEDROCK_SONNET_MODEL_ID.
    bedrock_sonnet_model_id: str = "us.anthropic.claude-sonnet-4-6"
    bedrock_sonnet_input_usd_per_mtok: float = 3.0
    bedrock_sonnet_output_usd_per_mtok: float = 15.0
    # Trailer reels keep only this speaker's sentences, matched against the
    # "Name, Company:" label Zoom puts on each transcript turn.
    trailer_presenter_name: str = "Paul Martin"

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
        "College Money Method Newsflash <newsflash@collegemoneymethod.com>,"
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

    # ── Webinar video pipeline (Zoom cloud recording → trimmed, chaptered Vimeo replay)
    # The ffmpeg work runs as a one-shot ECS Fargate task rather than in the API
    # container: on 1 vCPU it would starve uvicorn for the length of a 90-minute
    # transcode. Everything else in the pipeline is API calls made in-process.
    #
    # All five ECS values come from the cmm-infra task module. Leaving any of
    # them empty disables dispatch — the job row is still created and still
    # visible on the admin screen, it just never launches, which is the right
    # behaviour in local dev where there is no cluster to launch into.
    ecs_cluster_arn: str = ""
    video_task_definition_arn: str = ""
    # Comma-separated. Fargate needs at least one subnet with egress to Zoom,
    # Vimeo, S3 and Bedrock.
    video_task_subnets: str = ""
    video_task_security_group: str = ""
    # Container name inside the task definition — RunTask command overrides are
    # addressed by container name, so this must match the infra definition.
    video_task_container_name: str = "video-pipeline"
    # Ceiling on simultaneously running processing tasks. Jobs over the cap stay
    # `pending` and the sweeper dispatches them as capacity frees. Starts at 3:
    # enough to clear a burst inside the ~30-minute window that keeps Zoom cloud
    # storage near baseline, low enough not to saturate the Fargate quota.
    video_pipeline_max_concurrent: int = 3
    # Ops address alerted when a job fails. MUST be an alias that never appears
    # in a family send — suppression is un-bypassable in ses_client, so one
    # unsubscribe on a shared address would silently kill every future alert.
    # Empty means no email is sent; failures then surface only on the admin
    # monitoring screen, and the notifier logs at ERROR to say so.
    video_pipeline_alert_email: str = ""
    # Domains registered on each uploaded video's embed whitelist. Vimeo privacy
    # is embed-only (view=disable + embed=whitelist), and a whitelist with zero
    # domains blocks the player everywhere — so registration is part of the
    # upload, not a follow-up step. Comma-separated. The apex alone is registered
    # on the assumption that Vimeo's whitelist covers subdomains, which is what
    # lets one entry serve www., next. and dev.next.; add them explicitly if a
    # replay ever refuses to play on one.
    vimeo_embed_domains: str = "collegemoneymethod.com"
    # Vimeo user whose library new videos are created in, as a URI
    # ("/users/151255816"). Empty means the token owner's own library ("/me").
    # Uploading elsewhere is only possible when the API app behind the token is
    # owned by — or OAuth-authorised on — that account; Vimeo refuses with
    # "This app can only upload to the app owner's account" otherwise.
    vimeo_upload_user_uri: str = ""
    # Folder ("project") that audit runs are filed into, as a URI
    # ("/users/151255816/projects/30467578"). Audit runs exist to be reviewed by
    # a human, so they are kept out of the library the production replays land
    # in. Only the seed: an admin overrides it in Global Settings
    # (app_config.vimeo_audit_folder_uri), and a cleared override falls back
    # here. Both empty means an audit run refuses to upload rather than
    # scattering unreviewed videos into the main library.
    vimeo_audit_folder_uri: str = ""

    # Frame sampling. These are the tunables of the one ffmpeg pass that decides
    # what the vision model ever sees, so they are named configuration rather
    # than literals buried in the filter string.
    #
    # The crop keeps the top-left region and discards the burned-in overlays that
    # change constantly — speaker PiP (top-right), Zoom's per-second clock
    # (bottom-right), rolling captions (bottom-centre). It is not an
    # optimisation: with an overlay left in, every sampled frame differs from the
    # last, mpdecimate drops nothing, and a 90-minute recording yields thousands
    # of "distinct states" instead of ~150.
    #
    # UNVERIFIED: both fractions are estimated from screenshots and have not yet
    # been checked against a frame extracted from a real recording. Confirm with
    # scripts/debug/video_pipeline_local.py before trusting an unattended run —
    # a candidate count in the thousands is the symptom of a wrong value.
    video_overlay_crop_width_fraction: float = 0.83
    video_overlay_crop_height_fraction: float = 0.88
    # 2 fps gives a title card held "a couple of seconds" 2-4 samples; 1 fps
    # gives 1-2, which is too tight to rely on.
    video_sample_fps: float = 2.0
    # How much of the picture must change before a sampled frame counts as a new
    # visual state. ffmpeg scores each frame against the one before it on 0..1.
    #
    # Measured over a real 82-minute workshop (9,889 frames at 2 fps): the median
    # score is 0.0001 and the 90th percentile 0.015, because the presenter's
    # camera fills the frame and moving is not changing. A slide or title card
    # replaces the whole picture and scores an order of magnitude higher. The
    # curve at this threshold keeps 229 frames; 0.02 keeps 709 and 0.10 keeps 148.
    #
    # Set low within that gap on purpose. Too high loses a transition and with it
    # a chapter boundary; too low only costs vision calls, which the ceiling
    # below already bounds.
    video_sample_scene_threshold: float = 0.05
    # Wide enough to read a large heading after cropping, small enough to keep
    # the vision call cheap.
    video_sample_width: int = 960
    # Hard ceiling on the candidate set. Everything after sampling costs money
    # per frame — an S3 PUT, an S3 GET and a Bedrock vision call each — so a
    # filter that stops discriminating does not produce worse chapters, it
    # produces a bill. The sampling filter targets ~150 for a 90-minute
    # recording; this is set well above that so it only ever catches a genuine
    # malfunction, and the job fails rather than classifying past it.
    video_max_sample_frames: int = 1000

    # Chaptering. The three recurring segments — introduction, resource center
    # tour, Q&A — carry no title card, so their labels are configuration rather
    # than model output: they are identical every week and 43 webinars a month
    # of school-facing pages should not alternate between "Q&A", "Questions
    # from families" and "Audience Q&A".
    video_chapter_label_introduction: str = "Introduction"
    video_chapter_label_tour: str = "Resource center tour"
    video_chapter_label_qna: str = "Q&A"
    # A brief screen share is a detour; a long one is the resource center tour.
    # A starting guess, to be tuned on the first real recordings the same way as
    # the crop fractions above.
    video_tour_min_seconds: float = 120.0
    # How far back into the final slide section to look for the sentence that
    # opens the Q&A. The camera cut lands on the sampling grid, but the
    # presenter announces the Q&A a little earlier, while the last slide is
    # still up. Bounded so an early "we'll take questions at the end" cannot
    # match.
    video_qna_lookback_seconds: float = 180.0
    # Vimeo's real ceiling is undocumented in what we checked, so cap
    # defensively and log the truncation rather than have the API reject a list.
    video_max_chapters: int = 40
    # Half-width of the transcript window searched around a title-card chapter
    # for the words of its own title. Advisory only: a no-match flags a chapter
    # for a human glance, it never drops one.
    video_chapter_crosscheck_seconds: float = 60.0

    # Chaptering reads the transcript first and the frames second. The deck is
    # not a table of contents: a real webinar opens with a title slide, a bio
    # slide and an agenda inside the first four minutes, and drops a
    # heading-only slide mid-section whenever the presenter changes emphasis.
    # Promoting every one of those produced eleven chapters where a viewer
    # wanted seven. What the speaker is *doing* separates the two cases, so a
    # topic pass over the cues decides where the sections are and the frames
    # only name them.
    #
    # Window searched around a transcript boundary for the slide that titles it.
    # Asymmetric because the presenter usually advances the slide and then
    # starts talking, so the card leads the words more often than it trails
    # them.
    video_topic_window_before_seconds: float = 90.0
    video_topic_window_after_seconds: float = 45.0
    # Shortest gap allowed between two consecutive content sections. A guard on
    # the topic pass, and the whole defence in the frames-only fallback, where
    # nothing else can tell a section from a sub-heading. The recurring
    # segments are exempt: a tour that runs straight into the Q&A is two real
    # chapters two minutes apart.
    video_min_section_seconds: float = 240.0
    # Upper bound on what the topic pass may return, before the chapter cap.
    # A reply longer than this is not a granular answer, it is a broken one.
    video_max_sections: int = 15
    # Cues are merged into blocks of about this length before the topic pass
    # sees them. Zoom emits a cue per sentence, so an 80-minute session is
    # ~1,500 of them; merged, it is ~250 numbered lines the model can hold in
    # view at once. Resolution lost here costs nothing — the frame window is
    # wider than the block, and the title card sets the final timecode.
    video_topic_block_seconds: float = 20.0

    # Translated captions are made after the replay is published, not before.
    # Vimeo generates the English transcript itself and offers no webhook to
    # announce it, so the only way to know it exists is to look — and holding a
    # finished video back for hours while polling would delay the thing schools
    # are actually waiting for.
    #
    # Which languages the English track is translated into. Codes must be keys
    # of SUPPORTED_LOCALES; anything else is dropped with a warning rather than
    # failing the run.
    video_caption_locales: str = "es,zh,zh-Hant"
    # How many sweeps to keep looking for Vimeo's English track before giving
    # up. At one sweep every five minutes this is about four hours, comfortably
    # past the usual wait, and the give-up is recorded as `skipped` rather than
    # `failed`: no transcript is a Vimeo outcome, not a broken pipeline.
    video_caption_max_attempts: int = 48

    # S3 prefixes for pipeline artefacts, both under the app bucket.
    # `originals` holds the untrimmed source and is what makes a job re-runnable
    # once Zoom's copy is gone; `frames` holds the candidate JPEGs plus
    # candidates.json and transcript.json that chaptering reads.
    video_archive_prefix: str = "video-pipeline/originals"
    video_frames_prefix: str = "video-pipeline/frames"
    # Storage class for the archived original. Glacier Instant Retrieval bills
    # $0.004/GB-month with millisecond GETs, and its 90-day minimum billing
    # duration matches the retention exactly, so the usual short-retention
    # penalty does not apply.
    video_archive_storage_class: str = "GLACIER_IR"
    # Must match the bucket lifecycle rule in cmm-infra. Application code never
    # deletes an original — this value only tells the admin screen when retry
    # has stopped being possible.
    video_archive_retention_days: int = 90
    # How long to wait for Vimeo to finish transcoding before failing the job.
    # A 90-minute upload usually clears in a few minutes; the ceiling exists so
    # a stuck transcode surfaces as a failure rather than as a task that never
    # exits.
    video_transcode_timeout_seconds: int = 1800

    # Airtable sync — offboarding safety. When False, the counselor revoke pass
    # runs in log-only mode (reports what WOULD be revoked without acting). Flip
    # to True after reviewing the first-deploy logs to enable live revocation.
    sync_enable_revoke: bool = False
    # Skip contact deactivation if more than this fraction of known Airtable-linked
    # contacts are missing from a pull (guards against partial/failed Airtable fetch).
    sync_deactivation_max_missing_fraction: float = 0.1


settings = Settings()
