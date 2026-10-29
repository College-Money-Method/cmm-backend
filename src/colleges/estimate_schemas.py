"""Shapes for POST /colleges/public/estimates. Money is JSON numbers; null = not enough data."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.colleges.json_decimal import JsonDecimal


class EstimateQueryIn(BaseModel):
    """Family financial inputs. Sent in the body, not the URL, so access logs never see them."""

    model_config = ConfigDict(extra="forbid")

    sai: int = Field(ge=-1500, le=999999)
    home_state: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}$")
    merit: str = Field(default="avg", pattern=r"^(none|avg|\d{1,6})$")


class EstimateInputsOut(BaseModel):
    sai: int
    home_state: str | None
    merit_mode: Literal["none", "avg", "amount"]
    merit_amount: JsonDecimal | None


class EstimateItem(BaseModel):
    college_id: uuid.UUID
    coa_used: JsonDecimal | None
    coa_basis: Literal["in_state", "out_of_state"] | None
    coa_year: str | None
    need: JsonDecimal | None
    need_package: JsonDecimal | None
    merit_used: JsonDecimal | None
    net_low: JsonDecimal | None
    net_high: JsonDecimal | None
    self_help_low: JsonDecimal | None
    self_help_high: JsonDecimal | None
    family_low: JsonDecimal | None
    family_high: JsonDecimal | None
    missing: list[str]


class EstimateListOut(BaseModel):
    inputs: EstimateInputsOut
    items: list[EstimateItem]
