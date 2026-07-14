"""Unit tests for the pure Airtable-sync helpers (no DB required)."""

from src.schools.sync_utils import (
    deactivation_is_safe,
    detect_email_collisions,
    should_revoke_access,
)


def _rec(rec_id: str, email: str | None) -> dict:
    return {"id": rec_id, "fields": ({"Email": email} if email is not None else {})}


class TestDetectEmailCollisions:
    def test_no_collisions(self):
        records = [_rec("r1", "a@x.com"), _rec("r2", "b@x.com")]
        assert detect_email_collisions(records) == {}

    def test_collision_case_insensitive_and_grouped(self):
        records = [_rec("r1", "Dup@X.com"), _rec("r2", "dup@x.com"), _rec("r3", "c@x.com")]
        result = detect_email_collisions(records)
        assert result == {"dup@x.com": ["r1", "r2"]}

    def test_ignores_blank_and_missing_email(self):
        records = [_rec("r1", ""), _rec("r2", None), _rec("r3", "  ")]
        assert detect_email_collisions(records) == {}


class TestDeactivationIsSafe:
    def test_no_known_contacts_is_safe(self):
        assert deactivation_is_safe(set(), set(), 0.2) is True

    def test_empty_pull_with_known_contacts_is_unsafe(self):
        assert deactivation_is_safe(set(), {"a", "b"}, 0.2) is False

    def test_within_threshold_is_safe(self):
        # 1 of 10 missing = 0.1 <= 0.2
        known = {f"r{i}" for i in range(10)}
        pulled = known - {"r0"}
        assert deactivation_is_safe(pulled, known, 0.2) is True

    def test_over_threshold_is_unsafe(self):
        # 5 of 10 missing = 0.5 > 0.2
        known = {f"r{i}" for i in range(10)}
        pulled = {"r0", "r1", "r2", "r3", "r4"}
        assert deactivation_is_safe(pulled, known, 0.2) is False


class TestShouldRevokeAccess:
    ACTIVE = {"active-user"}
    MANAGED = {"active-user", "inactive-user"}

    def test_super_admin_never_revoked(self):
        assert should_revoke_access("inactive-user", "super_admin", self.ACTIVE, self.MANAGED) is False

    def test_unmanaged_role_never_revoked(self):
        # admin-created role, no backing contact
        assert should_revoke_access("admin-created", "hub_user", self.ACTIVE, self.MANAGED) is False

    def test_active_managed_role_kept(self):
        assert should_revoke_access("active-user", "hub_user", self.ACTIVE, self.MANAGED) is False

    def test_inactive_managed_role_revoked(self):
        assert should_revoke_access("inactive-user", "hub_admin", self.ACTIVE, self.MANAGED) is True
