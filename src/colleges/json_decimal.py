"""Decimal that serializes to a JSON number (Pydantic v2 sends plain Decimal as a string).

Money is stored to cents and percents to 4 places, so a float carries the value
exactly enough for display, sort and math in the explorer UI. Python code keeps the
exact Decimal; only the JSON form changes.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer

JsonDecimal = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]
