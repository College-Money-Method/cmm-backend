"""Cell-value parsing and formatting for the CMM aid workbook converter.

Kept apart from the converter so the sheet walk stays readable. Every helper is
pure: workbook cell in, contract-shaped text (or an error) out.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal

YEAR_RE = re.compile(r"^(\d{4})-(\d{2})$")


class ConversionError(Exception):
    pass


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip() if value is not None else ""


def num(value, field: str, row: int):
    """Number or None. 'N/R' and blank mean not reported; anything else is an error."""
    if value is None or (isinstance(value, str) and clean(value).upper() in ("", "N/R")):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConversionError(f"row {row}: {field} is not a number: {value!r}")
    return value


def fmt_dec(value, places: str) -> str:
    """Plain decimal text rounded half-up (percents to 4 places, money to 2); None -> empty."""
    if value is None:
        return ""
    return format(Decimal(repr(value)).quantize(Decimal(places), rounding=ROUND_HALF_UP).normalize(), "f")


def fmt_pct(value) -> str:
    return fmt_dec(value, "0.0001")


def fmt_money(value) -> str:
    return fmt_dec(value, "0.01")


def parse_year(raw, row: int) -> tuple[str, str]:
    """(cds_year, data_note). Empty year = not reported; the note says why."""
    if isinstance(raw, (dt.datetime, dt.date)):
        return "", f"CDS year N/R in source (workbook cell held an Excel date, {raw:%Y-%m-%d})"
    text = clean(raw).replace("–", "-")
    if text.upper() in ("", "N/R"):
        return "", "CDS year N/R in source"
    m = YEAR_RE.match(text)
    if not m or int(m.group(2)) != (int(m.group(1)) + 1) % 100:
        raise ConversionError(f"row {row}: invalid CDS year {raw!r}")
    return text, ""
