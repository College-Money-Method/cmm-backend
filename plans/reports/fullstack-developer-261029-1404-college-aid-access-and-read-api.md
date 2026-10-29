# College Aid access + latest-aid read API (backend)

## Files
Created src/colleges: access_password.py, access_token.py, latest_query.py, derived_metrics.py, public_router.py, public_schemas.py
Created tests/colleges: test_access_password, test_access_token, test_latest_query, test_derived_metrics, test_public_router, test_app_config_college_aid_password (75 tests incl. pre-existing colleges tests; ~58 new, all pass)
Modified: src/app_config/router.py (PUT/DELETE /college-aid-password, 204), src/app_config/schemas.py (CollegeAidPasswordIn; AppConfigOut gains college_aid_password_set + college_aid_password_updated_at; hash not a field), src/schools/src_session.py (signing_secret())

## Notes
- Reusable dep: `CollegeAidAccessDep` / `require_college_aid_access` in src/colleges/access_token.py (token header or super_admin bearer).
- main.py NOT touched: wire `from src.colleges.public_router import router`. Tests build their own FastAPI app for it.
- Delay patchable via `public_router.BAD_PASSWORD_DELAY_SECONDS`.
- Decimals serialize as JSON strings (pydantic v2 default); frontend normalizer already coerces.
- aid_form rules: contains "css" -> css (wins over fafsa); "fafsa" -> fafsa; "institution"/"form" -> institutional; other text -> other; blank -> null.
- Token with password cleared -> 401 college_aid_token_invalid (not 403).

## Test status
tests/colleges: 75 pass. Full suite: 1827 pass, 2 fail + 1 collection error, all pre-existing/unrelated (analytics: test_content_endpoint_shape, test_content_video_views_scoped_to_topic KeyError 'other_videos'; test_resource_breakdown_queries ImportError resolve_video_rows). ruff not installed in venv; not run.

## Open questions
- Is the mixed "FAFSA and CSS" -> css mapping what product wants?
- AppConfigOut built via before-validator reading ORM attrs; any code constructing it from non-ORM objects should still work (dict passthrough).
