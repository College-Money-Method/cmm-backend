"""Resource rankings are keyed by asset id, then named from Postgres.

Grouping by asset_name merged distinct assets that share a title (live data had
40 asset ids behind 30 names) and split an asset's history across a rename.
"""

import uuid

from src.analytics.resource_breakdown_queries import (
    get_school_slug,
    resolve_asset_rows,
    resolve_video_rows,
)
from src.analytics.schemas import TopBreakdown


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """Returns the configured rows for any query; the SQL itself is SQLAlchemy's."""

    def __init__(self, rows=()):
        self._rows = list(rows)
        self.queries = 0

    def execute(self, _stmt):
        self.queries += 1
        return _Result(self._rows)


class TestResolveAssetRows:
    def test_names_come_from_postgres_not_the_event(self):
        """The CURRENT name is shown, so a rename relabels the whole history
        instead of splitting it into two rows."""
        aid = uuid.uuid4()
        db = _FakeSession([(aid, "FAFSA Checklist (2027)")])
        rows = resolve_asset_rows(db, [TopBreakdown(label=str(aid), count=12.0)])
        assert rows[0].id == str(aid)
        assert rows[0].name == "FAFSA Checklist (2027)"
        assert rows[0].count == 12

    def test_order_is_preserved(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        db = _FakeSession([(a, "First"), (b, "Second")])
        rows = resolve_asset_rows(db, [
            TopBreakdown(label=str(a), count=9.0),
            TopBreakdown(label=str(b), count=4.0),
        ])
        assert [r.name for r in rows] == ["First", "Second"]

    def test_deleted_asset_keeps_its_count_and_is_unlinked(self):
        """Dropping the row would make the list disagree with the Resources Used
        tile; the link is dropped instead, since the page would 404."""
        aid = uuid.uuid4()
        db = _FakeSession([])  # nothing found
        rows = resolve_asset_rows(db, [TopBreakdown(label=str(aid), count=7.0)])
        assert rows[0].count == 7
        assert rows[0].id is None
        assert "Removed resource" in rows[0].name
        assert str(aid)[:8] in rows[0].name  # distinguishes several removed assets

    def test_non_uuid_label_is_not_queried(self):
        db = _FakeSession([])
        rows = resolve_asset_rows(db, [TopBreakdown(label="not-an-id", count=3.0)])
        assert db.queries == 0  # nothing worth looking up
        assert rows[0].id is None
        assert rows[0].count == 3

    def test_no_rows_no_query(self):
        db = _FakeSession([])
        assert resolve_asset_rows(db, []) == []
        assert db.queries == 0


class TestResolveVideoRows:
    """The "Other Videos" list: resource videos link to their page (like the
    resources card); the welcome video is a plain, unlinked row."""

    def test_resource_video_links_like_a_resource(self):
        aid = uuid.uuid4()
        db = _FakeSession([(aid, "College Costs 101")])
        rows = resolve_video_rows(db, [TopBreakdown(label=str(aid), count=15.0)])
        assert rows[0].id == str(aid)          # linkable to /resources/{id}
        assert rows[0].name == "College Costs 101"
        assert rows[0].count == 15

    def test_welcome_video_is_named_and_unlinked(self):
        db = _FakeSession([])
        rows = resolve_video_rows(db, [TopBreakdown(label="welcome-video", count=7.0)])
        assert rows[0].id is None              # no resource page
        assert rows[0].name == "Welcome Video"
        assert rows[0].count == 7

    def test_mixed_order_preserved(self):
        aid = uuid.uuid4()
        db = _FakeSession([(aid, "Scholarships")])
        rows = resolve_video_rows(db, [
            TopBreakdown(label="welcome-video", count=9.0),
            TopBreakdown(label=str(aid), count=4.0),
        ])
        assert [(r.name, r.id) for r in rows] == [
            ("Welcome Video", None),
            ("Scholarships", str(aid)),
        ]


class TestGetSchoolSlug:
    def test_returns_slug(self):
        db = _FakeSession(["lincoln-high"])
        assert get_school_slug(db, str(uuid.uuid4())) == "lincoln-high"

    def test_no_school_means_no_links(self):
        """Admin viewing all schools: rows render unlinked rather than pointing at
        an arbitrary school's copy of the resource."""
        db = _FakeSession(["lincoln-high"])
        assert get_school_slug(db, None) is None
        assert get_school_slug(db, "not-a-uuid") is None
        assert db.queries == 0
