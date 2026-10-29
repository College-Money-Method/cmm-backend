import pytest

from src.colleges.name_key import normalize_name


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Adelphi University", "adelphi university"),
        ("  Coastal Carolina University ", "coastal carolina university"),
        ("King's College", "kings college"),
        ("Kings College", "kings college"),
        ("Texas A&M University", "texas a and m university"),
        ("Texas A & M University", "texas a and m university"),
        ("St.  John's   College", "st johns college"),
        ("Embry Riddle Aeronautical U. - Daytona", "embry riddle aeronautical u daytona"),
        ("", ""),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_normalize_is_idempotent():
    once = normalize_name("Bard College at Simon's Rock")
    assert normalize_name(once) == once
