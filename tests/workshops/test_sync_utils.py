"""Unit tests for the pure webinar-sync helpers (no DB required)."""

from src.workshops.sync_utils import select_stale_mapping_pairs

# Compact pair aliases: (school_id, webinar_id)
A = ("s1", "w1")
B = ("s2", "w1")
C = ("s3", "w1")


class TestSelectStaleMappingPairs:
    def test_nothing_stale_when_db_matches_airtable(self):
        stale, tripped = select_stale_mapping_pairs([A, B], {A, B}, 0.1)
        assert stale == []
        assert tripped is False

    def test_removed_school_is_stale(self):
        # Airtable dropped B -> B is stale, 1/2 = 50% but guard high enough here
        stale, tripped = select_stale_mapping_pairs([A, B], {A}, 0.9)
        assert stale == [B]
        assert tripped is False

    def test_guard_trips_when_stale_fraction_too_high(self):
        # 1 of 2 stale = 50% > 10% default -> guard trips, caller deletes nothing
        stale, tripped = select_stale_mapping_pairs([A, B], {A}, 0.1)
        assert stale == [B]
        assert tripped is True

    def test_single_removal_in_large_set_under_guard(self):
        existing = [("s%d" % i, "w1") for i in range(20)]  # 20 pairs
        desired = set(existing[1:])  # drop exactly one -> 1/20 = 5% <= 10%
        stale, tripped = select_stale_mapping_pairs(existing, desired, 0.1)
        assert stale == [existing[0]]
        assert tripped is False

    def test_empty_existing_is_safe_noop(self):
        stale, tripped = select_stale_mapping_pairs([], {A}, 0.1)
        assert stale == []
        assert tripped is False

    def test_boundary_fraction_equal_is_not_tripped(self):
        # exactly at threshold (1/10 == 0.1) must NOT trip (uses strict >)
        existing = [("s%d" % i, "w1") for i in range(10)]
        desired = set(existing[1:])  # 1/10 = 0.1
        stale, tripped = select_stale_mapping_pairs(existing, desired, 0.1)
        assert stale == [existing[0]]
        assert tripped is False
