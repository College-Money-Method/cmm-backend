"""Pure value rules shared by the CSV import and the admin edit forms.

One rule set: every parser takes the raw cell/JSON value and returns the clean
value (``None`` = not reported) or raises ``RuleError`` with a message that is
safe to show (echoed input is capped, no exception text is ever passed on).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from src.colleges.models import REGIONS

# Exact CSV header set (order does not matter).
COLUMNS = (
    "college_id", "name", "state", "region", "control", "coa_in_state", "coa_out_of_state", "coa_year",
    "coa_estimated", "full_time_undergrads", "aid_application", "pct_need_met",
    "pct_non_need_receiving_merit", "avg_merit_award", "cds_year", "source", "release_label", "data_notes",
)
# Columns a file may add to COLUMNS; older files without them stay valid.
OPTIONAL_COLUMNS = ("unitid",)
# Record facts an admin may edit by hand or the CSV may refresh (cds_year/release_label handled apart).
FACT_FIELDS = (
    "coa_in_state", "coa_out_of_state", "coa_year", "coa_estimated", "full_time_undergrads", "aid_application",
    "pct_need_met", "pct_non_need_receiving_merit", "avg_merit_award", "source", "data_notes",
)
ECHO_MAX = 50
_STATES_BY_REGION = {
    "Great Lakes": "IL IN MI OH WI",
    "Mid-Atlantic": "DC DE MD NJ NY PA",
    "New England": "CT MA ME NH RI VT",
    "Plains": "IA KS MN MO ND NE SD",
    "Rocky Mts": "CO ID MT UT WY",
    "Southeast": "AL AR FL GA KY LA MS NC SC TN VA WV PR",
    "Southwest": "AZ NM OK TX",
    "West Coast": "AK CA HI NV OR WA",
}
REGION_BY_STATE = {s: region for region, states in _STATES_BY_REGION.items() for s in states.split()}
assert set(_STATES_BY_REGION) == set(REGIONS)

_EMPTY = {"", "n/r", "nr"}
_YEAR = re.compile(r"^(\d{4})-(\d{2}|\d{4})$")
_RELEASE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_FORMULA_START = ("=", "+", "-", "@")
_TEXT_LIMITS = {"name": 200, "aid_application": 100, "source": 200, "data_notes": 1000}
_MONEY_MAX = Decimal("99999999.99")


class RuleError(ValueError):
    """A value broke a rule; ``str(error)`` is the user-facing message."""


def echo(value: Any) -> str:
    """Input echoed back in a message, capped so a hostile cell cannot bloat a response."""
    text = str(value)
    return repr(text if len(text) <= ECHO_MAX else text[:ECHO_MAX] + "...")


def clean(value: Any) -> Any:
    """Strip text; empty / N/R / NR -> None."""
    if isinstance(value, str):
        value = value.strip()
        return None if value.lower() in _EMPTY else value
    return value


def _decimal(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise RuleError(f"not a number: {echo(value)}")
    try:
        number = Decimal(str(value).replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        raise RuleError(f"not a number: {echo(value)}") from None
    if not number.is_finite():
        raise RuleError(f"not a number: {echo(value)}")
    return number


def parse_money(value: Any) -> Decimal | None:
    value = clean(value)
    if value is None:
        return None
    number = _decimal(value)
    if number < 0 or number > _MONEY_MAX:
        raise RuleError(f"amount out of range: {echo(value)}")
    return number.quantize(Decimal("0.01"), ROUND_HALF_UP)


def parse_pct(value: Any) -> Decimal | None:
    value = clean(value)
    if value is None:
        return None
    if isinstance(value, str) and "%" in value:
        raise RuleError(f"use a fraction between 0 and 1, not a percent sign: {echo(value)}")
    number = _decimal(value)
    if number < 0 or number > 1:
        raise RuleError(f"must be a fraction between 0 and 1 (0.85, not 85): {echo(value)}")
    return number.quantize(Decimal("0.0001"), ROUND_HALF_UP)


def parse_count(value: Any) -> int | None:
    value = clean(value)
    if value is None:
        return None
    number = _decimal(value)
    if number != number.to_integral_value() or number < 0 or number > 2_000_000_000:
        raise RuleError(f"must be a non-negative whole number: {echo(value)}")
    return int(number)


def parse_year(value: Any) -> str | None:
    """'YYYY-YY' (also 'YYYY-YYYY'), second year = first + 1; Excel dates fail the pattern."""
    value = clean(value)
    if value is None:
        return None
    match = _YEAR.match(str(value))
    if not match:
        raise RuleError(f"not an academic year (expected YYYY-YY): {echo(value)}")
    first, second = int(match.group(1)), match.group(2)
    expected = first + 1
    if int(second) != (expected if len(second) == 4 else expected % 100):
        raise RuleError(f"years must be consecutive (e.g. 2025-26): {echo(value)}")
    if not 2000 <= first <= date.today().year + 1:
        raise RuleError(f"year out of range: {echo(value)}")
    return f"{first}-{expected % 100:02d}"


def parse_release(value: Any) -> str:
    value = clean(value)
    if value is None or not _RELEASE.match(str(value)):
        raise RuleError(f"release_label must look like YYYY-MM: {echo(value)}")
    return str(value)


def parse_bool(value: Any) -> bool | None:
    value = clean(value)
    if value is None or isinstance(value, bool):
        return value
    if str(value).lower() in ("true", "false"):
        return str(value).lower() == "true"
    raise RuleError(f"must be true or false: {echo(value)}")


def parse_unitid(value: Any) -> int | None:
    """Federal IPEDS UNITID: a 6-digit number, or blank for not known."""
    value = clean(value)
    if value is None:
        return None
    text = str(value).strip()
    # ASCII only: str.isdigit() accepts "²", which int() then rejects.
    if isinstance(value, bool) or not re.fullmatch(r"[1-9][0-9]{5}", text):
        raise RuleError(f"UNITID must be a 6-digit number: {echo(value)}")
    return int(text)


def parse_state(value: Any) -> str:
    value = clean(value)
    state = str(value).upper() if value is not None else ""
    if state not in REGION_BY_STATE:
        raise RuleError(f"not a known state code: {echo(value)}")
    return state


def parse_control(value: Any) -> str:
    value = clean(value)
    if value is None or str(value).lower() not in ("public", "private"):
        raise RuleError(f"must be public or private: {echo(value)}")
    return str(value).lower()


def text_parser(column: str, required: bool = False) -> Callable[[Any], str | None]:
    limit = _TEXT_LIMITS[column]

    def parse(value: Any) -> str | None:
        value = clean(value)
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                raise RuleError("required")
            return None
        if not isinstance(value, str) or len(value) > limit:
            raise RuleError(f"must be text of at most {limit} characters")
        return value

    return parse


# column -> parser for every record fact, shared by CSV rows and edit forms.
FACT_PARSERS: dict[str, Callable[[Any], Any]] = {
    "coa_in_state": parse_money, "coa_out_of_state": parse_money, "coa_year": parse_year,
    "coa_estimated": parse_bool, "full_time_undergrads": parse_count,
    "aid_application": text_parser("aid_application"), "pct_need_met": parse_pct,
    "pct_non_need_receiving_merit": parse_pct, "avg_merit_award": parse_money,
    "source": text_parser("source"), "data_notes": text_parser("data_notes"),
}
parse_name = text_parser("name", required=True)


def check_coa_year(values: dict[str, Any]) -> None:
    """COA present -> coa_year required."""
    if (values.get("coa_in_state") is not None or values.get("coa_out_of_state") is not None) and not values.get("coa_year"):
        raise RuleError("coa_year is required when a cost of attendance is given")
