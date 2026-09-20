"""Postgres aggregation for the admin Bedrock spend tab.

Feeds GET /api/v1/analytics/admin/bedrock.

There are two ledgers, for a historical reason worth knowing when reading these
numbers. ``translation_usage`` came first and is written by the translation
client; ``bedrock_usage`` is written by ``bedrock_client.call_json``, which
every other caller goes through. The two clients are separate code paths, so no
invocation lands in both and unioning them cannot double-count. Translation
rows are surfaced as ``translation:<context>`` so the tab shows one row per kind
of translation rather than collapsing the largest spender into a single line.

Everything here respects the same ``days`` window, including the headline
totals: a page whose big number covers all time while its chart covers a month
invites exactly the wrong conclusion. All-time cost is reported separately and
explicitly.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, literal, select, union_all
from sqlalchemy.orm import Session

from src.content.translation_models import TranslationUsage
from src.video_pipeline.bedrock_usage_models import BedrockUsage


def _window_start(days: int) -> datetime:
    return (datetime.now(timezone.utc) - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )


def _day_key(value) -> str:
    """Postgres returns a date object here, SQLite an ISO string. Take either."""
    return value if isinstance(value, str) else value.isoformat()


def _ledger():
    """Both ledgers as one row set: (invoke_type, cost_usd, tokens, created_at)."""
    own = select(
        BedrockUsage.invoke_type.label("invoke_type"),
        BedrockUsage.cost_usd.label("cost_usd"),
        BedrockUsage.input_tokens.label("input_tokens"),
        BedrockUsage.output_tokens.label("output_tokens"),
        BedrockUsage.created_at.label("created_at"),
    )
    translation = select(
        # `||` rather than concat(): renders on Postgres and SQLite alike.
        (literal("translation:") + TranslationUsage.context).label("invoke_type"),
        TranslationUsage.cost_usd.label("cost_usd"),
        TranslationUsage.input_tokens.label("input_tokens"),
        TranslationUsage.output_tokens.label("output_tokens"),
        TranslationUsage.created_at.label("created_at"),
    )
    return union_all(own, translation).subquery()


def get_totals(db: Session, days: int) -> dict:
    """Spend in the window, plus all-time cost for context."""
    led = _ledger()
    row = db.execute(
        select(
            func.coalesce(func.sum(led.c.cost_usd), 0),
            func.coalesce(func.sum(led.c.input_tokens), 0),
            func.coalesce(func.sum(led.c.output_tokens), 0),
            func.count(),
        ).where(led.c.created_at >= _window_start(days))
    ).one()
    all_time = db.scalar(select(func.coalesce(func.sum(led.c.cost_usd), 0))) or 0
    return {
        "cost_usd": float(row[0]),
        "input_tokens": int(row[1]),
        "output_tokens": int(row[2]),
        "invocations": int(row[3]),
        "all_time_cost_usd": float(all_time),
    }


def get_by_invoke_type(db: Session, days: int) -> list[dict]:
    """Spend grouped by which call site spent it, dearest first.

    ``avg_cost_usd`` is what one invocation of that kind costs. It is the number
    that answers "what will re-running this cost me", which a total alone never
    does: a type can dominate the bill through volume of cheap calls or through
    a handful of expensive ones, and the two want different responses.
    """
    led = _ledger()
    rows = db.execute(
        select(
            led.c.invoke_type,
            func.sum(led.c.cost_usd),
            func.sum(led.c.input_tokens),
            func.sum(led.c.output_tokens),
            func.count(),
        )
        .where(led.c.created_at >= _window_start(days))
        .group_by(led.c.invoke_type)
        .order_by(func.sum(led.c.cost_usd).desc())
    ).all()
    return [
        {
            "invoke_type": r[0],
            "cost_usd": float(r[1]),
            "input_tokens": int(r[2]),
            "output_tokens": int(r[3]),
            "invocations": int(r[4]),
            "avg_cost_usd": float(r[1]) / int(r[4]) if r[4] else 0.0,
        }
        for r in rows
    ]


def get_daily(db: Session, days: int) -> list[dict]:
    """Daily cost and tokens across both ledgers, gaps filled with zeros."""
    led = _ledger()
    since = _window_start(days)
    # date() rather than a cast: both Postgres and SQLite have it, and a
    # CAST to DATE is a no-op on SQLite that silently returns a number.
    day_col = func.date(led.c.created_at)
    rows = db.execute(
        select(
            day_col,
            func.sum(led.c.cost_usd),
            func.sum(led.c.input_tokens),
            func.sum(led.c.output_tokens),
        )
        .where(led.c.created_at >= since)
        .group_by(day_col)
    ).all()
    by_day = {
        _day_key(r[0]): {
            "cost_usd": float(r[1]),
            "input_tokens": int(r[2]),
            "output_tokens": int(r[3]),
        }
        for r in rows
    }
    empty = {"cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0}
    start = since.date()
    return [
        {"day": (d := (start + timedelta(days=i)).isoformat()), **by_day.get(d, empty)}
        for i in range(days)
    ]


def get_bedrock_analytics(db: Session, days: int = 30) -> dict:
    """Everything the tab needs, in one payload."""
    return {
        "totals": get_totals(db, days),
        "by_invoke_type": get_by_invoke_type(db, days),
        "daily": get_daily(db, days),
    }
