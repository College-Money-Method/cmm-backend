"""Video CC job runner: VTT → Bedrock translation → Vimeo text tracks.

Pipeline per job:
  1. Resolve + verify the Vimeo video (fails fast on 404/403).
  2. Parse the uploaded WebVTT/SRT into cues.
  3. Per target locale: translate cues in parallel chunks, rebuild the VTT,
     replace any existing track for that language, upload.

Only cue *text* reaches the model — timings are reassembled from the source in
``vtt_parser.serialize``. Each Bedrock call is logged to ``translation_usage``
under context ``video_cc``, so caption spend shows up alongside site translation
in the existing admin cost analytics.

Locales are independent: one language failing (Bedrock error, Vimeo rejection)
is reported on the stream and the remaining languages still run.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.orm import Session

from src.config import SUPPORTED_LOCALES
from src.content.bedrock_translation import TranslationError, translate_fields
from src.content.translation_service import record_translation_usage
from src.content.video_cc_jobs import VideoCcJob
from src.content.vtt_parser import Cue, VttDocument, VttError, chunk_cues, parse, serialize
from src.db.base import get_session_factory
from src.integrations import vimeo
from src.integrations.vimeo import VimeoError

logger = logging.getLogger(__name__)

# Ledger context for caption translations — a new value alongside
# "strings"/"topic"/"page"/"asset". get_by_context() groups without a whitelist,
# so this appears in the admin translation-cost view with no further changes.
_USAGE_CONTEXT = "video_cc"

# Chunk sizing mirrors the string-translation path: small batches fanned out
# beat few large ones on Haiku latency, and keep output under the 8k token cap.
_CUE_CHAR_BUDGET = 2500
_CUE_MAX_COUNT = 25
_MAX_CONCURRENT_CHUNKS = 6

# Haiku occasionally emits malformed JSON (an unescaped quote inside a translated
# string, a truncated object) — observed roughly 1 chunk in 4 on CJK output, and
# non-deterministic: the identical request succeeds on a retry. Retry before
# giving up, and when a chunk still fails let its cues keep their English text
# rather than failing the whole language.
_CHUNK_ATTEMPTS = 3


async def run_job(
    job: VideoCcJob,
    vtt_content: str | None,
    locales: list[str],
    replace_existing: bool = True,
    source_language: str = "en",
) -> None:
    """Execute a caption job, emitting progress events. Never raises.

    ``vtt_content`` of None means "use the track already on the video" — Vimeo's
    own AI or previously uploaded captions are downloaded and used as the source.
    """
    db: Session | None = None
    try:
        job.emit("status", message="Checking the video on Vimeo…")
        # Fail before spending anything if the token can't finish the job.
        missing = await asyncio.to_thread(vimeo.missing_scopes)
        if missing:
            raise VimeoError(
                f"The Vimeo access token is missing the {', '.join(missing)} "
                f"scope{'s' if len(missing) > 1 else ''}. Regenerate it at "
                f"developer.vimeo.com with all of: {vimeo.REQUIRED_SCOPES}."
            )

        video_name = await asyncio.to_thread(vimeo.get_video_name, job.video_ref)
        job.emit("video", name=video_name, video_ref=job.video_ref)

        if vtt_content is None:
            job.emit("status", message="Downloading the existing captions from Vimeo…")
            vtt_content, track_name = await asyncio.to_thread(
                vimeo.download_source_track, job.video_ref, source_language
            )
            job.emit("source", origin="vimeo", name=track_name)
        else:
            job.emit("source", origin="upload", name="Uploaded transcript")

        job.emit("status", message="Reading the caption file…")
        doc = parse(vtt_content)
        job.emit("parsed", cue_count=len(doc.cues))

        db = get_session_factory()()
        succeeded: list[str] = []
        failed: list[dict[str, str]] = []

        for locale in locales:
            try:
                result = await _process_locale(job, db, doc, locale, replace_existing)
                succeeded.append(locale)
                job.emit("language_done", locale=locale, **result)
            except (TranslationError, VimeoError, VttError) as exc:
                logger.warning("video_cc: %s failed for %s: %s", locale, job.video_ref, exc)
                db.rollback()
                failed.append({"locale": locale, "error": str(exc)})
                job.emit("language_error", locale=locale, error=str(exc))
            except Exception as exc:  # noqa: BLE001 — one language must not kill the job
                logger.exception("video_cc: unexpected failure for %s", locale)
                db.rollback()
                message = f"Unexpected error: {exc}"
                failed.append({"locale": locale, "error": message})
                job.emit("language_error", locale=locale, error=message)

        job.emit("done", succeeded=succeeded, failed=failed)
        job.finish("completed" if succeeded else "failed")

    except (VimeoError, VttError) as exc:
        # Fatal: no video or no parseable cues — nothing was uploaded or spent.
        job.emit("error", error=str(exc))
        job.finish("failed")
    except Exception as exc:  # noqa: BLE001 — the runner must never crash the loop
        logger.exception("video_cc: job %s crashed", job.id)
        job.emit("error", error=f"Unexpected server error: {exc}")
        job.finish("failed")
    finally:
        if db is not None:
            db.close()


async def _process_locale(
    job: VideoCcJob,
    db: Session,
    doc: VttDocument,
    locale: str,
    replace_existing: bool,
) -> dict[str, Any]:
    """Translate every cue into ``locale`` and publish the track to Vimeo."""
    language_name = SUPPORTED_LOCALES.get(locale, locale)
    job.emit("language_start", locale=locale, language=language_name)

    vimeo_code, track_name = await asyncio.to_thread(
        vimeo.resolve_language, locale, language_name
    )

    translations = await _translate_cues(job, db, doc.cues, locale, language_name)
    # Spend is durable even if the Vimeo upload below fails.
    db.commit()

    translated_vtt = serialize(doc, translations)

    replaced = False
    if replace_existing:
        replaced = await asyncio.to_thread(_remove_existing_tracks, job.video_ref, vimeo_code)

    job.emit("status", message=f"Uploading {language_name} captions to Vimeo…")
    track_uri = await asyncio.to_thread(
        vimeo.upload_text_track, job.video_ref, vimeo_code, track_name, translated_vtt
    )

    return {
        "language": language_name,
        "vimeo_code": vimeo_code,
        "track_uri": track_uri,
        "replaced": replaced,
        "cue_count": len(doc.cues),
    }


def _remove_existing_tracks(video_ref: str, vimeo_code: str) -> bool:
    """Delete existing tracks for this language so the upload is a replace.

    Returns True when at least one track was removed. Vimeo happily accepts two
    tracks with the same language, which would show duplicates in the player's
    caption menu, so this runs before every upload.
    """
    removed = False
    for track in vimeo.list_text_tracks(video_ref):
        if (track.get("language") or "").lower() == vimeo_code.lower():
            vimeo.delete_text_track(track["uri"])
            removed = True
    return removed


async def _translate_cues(
    job: VideoCcJob, db: Session, cues: list[Cue], locale: str, language_name: str
) -> dict[int, str]:
    """Translate all cues for one locale, in bounded-concurrency chunks.

    Returns ``{cue_index: translated_text}``. Cues the model omits are simply
    absent and fall back to source text at serialisation time.
    """
    batches = chunk_cues(cues, _CUE_CHAR_BUDGET, _CUE_MAX_COUNT)
    semaphore = asyncio.Semaphore(_MAX_CONCURRENT_CHUNKS)
    completed = 0

    async def run_batch(batch: list[Cue]):
        """Translate one chunk, retrying transient model errors.

        Returns ``(batch, TranslationOutput)`` on success or ``(batch, None)``
        when every attempt failed — a failed chunk must not abort its siblings.
        """
        async with semaphore:
            # Keys are cue indices as strings — the model returns the same keys,
            # which is how translated text finds its way back to the right timing.
            field_map = {str(cue.index): cue.text for cue in batch}
            for attempt in range(1, _CHUNK_ATTEMPTS + 1):
                try:
                    return batch, await asyncio.to_thread(translate_fields, field_map, locale)
                except TranslationError as exc:
                    logger.warning(
                        "video_cc: %s chunk (cues %d-%d) attempt %d/%d failed: %s",
                        locale,
                        batch[0].index,
                        batch[-1].index,
                        attempt,
                        _CHUNK_ATTEMPTS,
                        exc,
                    )
            return batch, None

    translations: dict[int, str] = {}
    failed_chunks = 0
    tasks = [asyncio.create_task(run_batch(batch)) for batch in batches]

    try:
        for future in asyncio.as_completed(tasks):
            batch, out = await future
            if out is None:
                failed_chunks += 1
            else:
                for key, value in out.fields.items():
                    # A blank value is treated as absent: the model occasionally
                    # returns "" for a cue, which would publish an empty caption
                    # at that timestamp instead of falling back to English.
                    if isinstance(value, str) and value.strip() and key.isdigit():
                        translations[int(key)] = value
                record_translation_usage(db, _USAGE_CONTEXT, locale, out, len(batch))

            completed += 1
            job.emit(
                "language_progress",
                locale=locale,
                language=language_name,
                completed=completed,
                total=len(batches),
            )
    except BaseException:
        # Cancel outstanding chunks so a failed locale stops burning Bedrock spend.
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    # Every chunk failing means something systemic (throttling, bad credentials) —
    # publishing an all-English "translation" would be worse than reporting it.
    if failed_chunks == len(batches):
        raise TranslationError(
            f"All {len(batches)} translation batches failed after "
            f"{_CHUNK_ATTEMPTS} attempts each."
        )

    missing = len(cues) - len(translations)
    if missing > 0:
        logger.warning(
            "video_cc: %d/%d cues missing from %s translation — keeping source text",
            missing,
            len(cues),
            locale,
        )
        job.emit("language_warning", locale=locale, missing_cues=missing)

    return translations
