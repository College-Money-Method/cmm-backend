"""The Bedrock spend ledger and what the admin tab reads off it.

The behaviours pinned here are the ones that decide whether the page tells the
truth: that a billed call is recorded even when its reply was useless, that
bookkeeping never takes a pipeline down with it, and that the two ledgers add
up to one number without counting anything twice.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from src.analytics.bedrock_usage_queries import (
    get_bedrock_analytics,
    get_by_invoke_type,
    get_daily,
    get_totals,
)
from src.content.translation_models import TranslationUsage
from src.db.base import Base
from src.video_pipeline import bedrock_client, bedrock_usage
from src.video_pipeline.bedrock_usage_models import BedrockUsage

TABLES = ("bedrock_usage", "translation_usage")


@pytest.fixture
def usage_sessionmaker():
    """In-memory SQLite holding just the two spend ledgers."""
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    metadata = MetaData()
    for name, table in Base.metadata.tables.items():
        if name in TABLES:
            table.to_metadata(metadata)
    metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def usage_db(usage_sessionmaker):
    session = usage_sessionmaker()
    try:
        yield session
    finally:
        session.close()


def _spend(db, invoke_type, cost, *, days_ago=0, tokens=(1000, 100)):
    db.add(
        BedrockUsage(
            id=uuid.uuid4(),
            invoke_type=invoke_type,
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            input_tokens=tokens[0],
            output_tokens=tokens[1],
            cost_usd=Decimal(str(cost)),
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
    )
    db.commit()


def _translation(db, context, cost, *, days_ago=0):
    db.add(
        TranslationUsage(
            id=uuid.uuid4(),
            context=context,
            locale="es",
            model_id="us.anthropic.claude-haiku-4-5-20251001-v1:0",
            input_tokens=500,
            output_tokens=200,
            item_count=3,
            cost_usd=Decimal(str(cost)),
            created_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        )
    )
    db.commit()


class TestCost:
    def test_cost_follows_the_configured_rates(self):
        # 1M in at $1 and 1M out at $5.
        assert bedrock_usage.cost_usd(1_000_000, 0) == Decimal("1.0")
        assert bedrock_usage.cost_usd(0, 1_000_000) == Decimal("5.0")

    def test_a_small_call_keeps_six_decimals_rather_than_rounding_to_nothing(self):
        # Frame classification is thousands of cheap calls. Rounding each to
        # cents would total them to zero and hide the largest caller entirely.
        assert bedrock_usage.cost_usd(800, 50) > 0


class TestRecord:
    def test_an_invocation_is_stored_with_its_cost(self, usage_db, usage_sessionmaker, monkeypatch):
        monkeypatch.setattr(bedrock_usage, "get_session_factory", lambda: usage_sessionmaker)
        bedrock_usage.record(bedrock_usage.FRAME_CLASSIFY, "model-x", 1_000_000, 0)

        row = usage_db.query(BedrockUsage).one()
        assert row.invoke_type == "frame_classify"
        assert row.cost_usd == Decimal("1.000000")

    def test_a_call_that_billed_nothing_writes_no_row(
        self, usage_db, usage_sessionmaker, monkeypatch
    ):
        # A zero row would inflate the invocation count with calls that never
        # reached the model, making the per-call average meaningless.
        monkeypatch.setattr(bedrock_usage, "get_session_factory", lambda: usage_sessionmaker)
        bedrock_usage.record(bedrock_usage.TRIM_POINT, "model-x", 0, 0)

        assert usage_db.query(BedrockUsage).count() == 0

    def test_a_failed_ledger_write_does_not_reach_the_caller(self, monkeypatch):
        # A video publish must not fail because an analytics insert did.
        def _broken():
            raise RuntimeError("database is down")

        monkeypatch.setattr(bedrock_usage, "get_session_factory", _broken)
        bedrock_usage.record(bedrock_usage.TOPIC_SEGMENT, "model-x", 10, 10)  # no raise


class TestByInvokeType:
    def test_both_ledgers_are_counted_once_each(self, usage_db):
        _spend(usage_db, "frame_classify", 2.00)
        _translation(usage_db, "strings", 3.00)

        rows = get_by_invoke_type(usage_db, days=30)
        types = {r["invoke_type"]: r["cost_usd"] for r in rows}

        assert types == {"frame_classify": 2.00, "translation:strings": 3.00}
        assert get_totals(usage_db, days=30)["cost_usd"] == 5.00

    def test_the_dearest_caller_is_listed_first(self, usage_db):
        _spend(usage_db, "qa_extraction", 1.00)
        _spend(usage_db, "frame_classify", 9.00)

        assert [r["invoke_type"] for r in get_by_invoke_type(usage_db, days=30)] == [
            "frame_classify",
            "qa_extraction",
        ]

    def test_the_average_separates_volume_from_expense(self, usage_db):
        # Same total, different shape: one dear call against ten cheap ones.
        _spend(usage_db, "topic_segment", 1.00)
        for _ in range(10):
            _spend(usage_db, "frame_classify", 0.10)

        by_type = {r["invoke_type"]: r for r in get_by_invoke_type(usage_db, days=30)}
        assert by_type["topic_segment"]["avg_cost_usd"] == pytest.approx(1.00)
        assert by_type["frame_classify"]["avg_cost_usd"] == pytest.approx(0.10)
        assert by_type["frame_classify"]["invocations"] == 10


class TestWindow:
    def test_spend_outside_the_window_is_left_out_of_the_headline(self, usage_db):
        _spend(usage_db, "qa_extraction", 1.00, days_ago=0)
        _spend(usage_db, "qa_extraction", 50.00, days_ago=90)

        totals = get_totals(usage_db, days=7)
        assert totals["cost_usd"] == 1.00
        # …but is never hidden: all-time is reported alongside it.
        assert totals["all_time_cost_usd"] == 51.00

    def test_every_day_in_the_window_appears_even_with_no_spend(self, usage_db):
        _spend(usage_db, "qa_extraction", 1.00)

        daily = get_daily(usage_db, days=7)
        assert len(daily) == 7
        assert sum(d["cost_usd"] for d in daily) == 1.00
        assert daily[0]["cost_usd"] == 0.0  # six days ago, nothing spent


class TestPayload:
    def test_an_empty_ledger_answers_with_zeros_rather_than_nothing(self, usage_db):
        payload = get_bedrock_analytics(usage_db, days=30)

        assert payload["totals"]["cost_usd"] == 0.0
        assert payload["totals"]["invocations"] == 0
        assert payload["by_invoke_type"] == []
        assert len(payload["daily"]) == 30


class TestRecordedAtTheClient:
    """The client records, so no caller can forget to."""

    @staticmethod
    def _reply(monkeypatch, text: str):
        """Stand in for a streamed Bedrock reply that billed 1000/100 tokens."""

        class _Message:
            content = [type("Block", (), {"text": text})()]
            usage = type("Usage", (), {"input_tokens": 1000, "output_tokens": 100})()

        class _Stream:
            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

            def get_final_message(self):
                return _Message()

        class _Client:
            messages = type("M", (), {"stream": staticmethod(lambda **_kw: _Stream())})()

        monkeypatch.setattr(bedrock_client, "get_client", lambda: _Client())

    def test_a_usable_reply_is_recorded_under_its_caller(self, monkeypatch):
        seen: list[tuple] = []
        monkeypatch.setattr(bedrock_client.bedrock_usage, "record", lambda *a: seen.append(a))
        self._reply(monkeypatch, '{"ok": true}')

        bedrock_client.call_json(
            system="s", content="c", invoke_type=bedrock_usage.QA_CLASSIFICATION
        )

        assert seen[0][0] == "qa_classification"
        assert seen[0][2:] == (1000, 100)

    def test_a_billed_reply_that_cannot_be_parsed_is_still_recorded(self, monkeypatch):
        # This is the spend most worth seeing: the call was paid for and threw
        # its answer away. A ledger written only on success would hide it.
        seen: list[tuple] = []
        monkeypatch.setattr(bedrock_client.bedrock_usage, "record", lambda *a: seen.append(a))
        self._reply(monkeypatch, "not json at all")

        with pytest.raises(bedrock_client.BedrockCallError):
            bedrock_client.call_json(
                system="s", content="c", invoke_type=bedrock_usage.FRAME_CLASSIFY
            )

        assert seen and seen[0][0] == "frame_classify"
