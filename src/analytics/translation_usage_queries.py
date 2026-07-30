"""Postgres aggregation queries for the admin Translation Analytics page.

Reads the translation_usage ledger (one row per Bedrock cache-miss invocation)
and the string_translations cache. Feeds GET /api/v1/analytics/admin/translation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Date, cast, func, select
from sqlalchemy.orm import Session

from src.content.translation_models import StringTranslation, TranslationUsage


def get_totals(db: Session) -> dict:
    """All-time totals across every recorded translation invocation."""
    row = db.execute(
        select(
            func.coalesce(func.sum(TranslationUsage.cost_usd), 0),
            func.coalesce(func.sum(TranslationUsage.input_tokens), 0),
            func.coalesce(func.sum(TranslationUsage.output_tokens), 0),
            func.count(),
        )
    ).one()
    cached_strings = db.scalar(select(func.count()).select_from(StringTranslation)) or 0
    return {
        "cost_usd": float(row[0]),
        "input_tokens": int(row[1]),
        "output_tokens": int(row[2]),
        "invocations": int(row[3]),
        "cached_strings": int(cached_strings),
    }


def get_by_locale(db: Session) -> list[dict]:
    """Spend + tokens grouped by locale, highest cost first."""
    rows = db.execute(
        select(
            TranslationUsage.locale,
            func.sum(TranslationUsage.cost_usd),
            func.sum(TranslationUsage.input_tokens),
            func.sum(TranslationUsage.output_tokens),
            func.count(),
        )
        .group_by(TranslationUsage.locale)
        .order_by(func.sum(TranslationUsage.cost_usd).desc())
    ).all()
    return [
        {
            "locale": r[0],
            "cost_usd": float(r[1]),
            "input_tokens": int(r[2]),
            "output_tokens": int(r[3]),
            "invocations": int(r[4]),
        }
        for r in rows
    ]


def get_by_context(db: Session) -> list[dict]:
    """Spend grouped by what was translated.

    Contexts written by the pipelines: ``strings`` (site-wide DOM translation),
    ``topic`` / ``page`` / ``asset`` (content entities), ``video_cc`` (caption
    tracks). No whitelist here — a new context appears automatically. Display
    names are mapped in the frontend; the stored codes are the data.
    """
    rows = db.execute(
        select(
            TranslationUsage.context,
            func.sum(TranslationUsage.cost_usd),
            func.sum(TranslationUsage.input_tokens),
            func.sum(TranslationUsage.output_tokens),
            func.count(),
        )
        .group_by(TranslationUsage.context)
        .order_by(func.sum(TranslationUsage.cost_usd).desc())
    ).all()
    return [
        {
            "context": r[0],
            "cost_usd": float(r[1]),
            "input_tokens": int(r[2]),
            "output_tokens": int(r[3]),
            "invocations": int(r[4]),
        }
        for r in rows
    ]


def get_daily(db: Session, days: int = 30) -> list[dict]:
    """Daily cost + tokens for the last `days` days, gaps filled with zeros."""
    since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day_col = cast(TranslationUsage.created_at, Date)
    rows = db.execute(
        select(
            day_col,
            func.sum(TranslationUsage.cost_usd),
            func.sum(TranslationUsage.input_tokens),
            func.sum(TranslationUsage.output_tokens),
        )
        .where(TranslationUsage.created_at >= since)
        .group_by(day_col)
    ).all()
    by_day = {
        r[0].isoformat(): {
            "cost_usd": float(r[1]),
            "input_tokens": int(r[2]),
            "output_tokens": int(r[3]),
        }
        for r in rows
    }
    out: list[dict] = []
    start_date = since.date()
    for i in range(days):
        d = (start_date + timedelta(days=i)).isoformat()
        entry = by_day.get(d, {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0})
        out.append({"day": d, **entry})
    return out


def get_translation_analytics(db: Session, days: int = 30) -> dict:
    """Everything the analytics page needs, in one payload."""
    return {
        "totals": get_totals(db),
        "by_locale": get_by_locale(db),
        "by_context": get_by_context(db),
        "daily": get_daily(db, days),
    }
