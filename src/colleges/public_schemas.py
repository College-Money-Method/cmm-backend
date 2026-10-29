"""Response shapes for the College Aid Explorer public API.

The JSON shape is a stable contract: the explorer UI and a future chatbot read
it. Percents are fractions 0..1, money is dollars, null means "not reported".
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from src.colleges.json_decimal import JsonDecimal

AidForm = Literal["fafsa", "css", "institutional", "other"]


class CollegeAidAccessIn(BaseModel):
    password: str = Field(max_length=200, repr=False)


class CollegeAidAccessOut(BaseModel):
    token: str
    expires_at: datetime


class CollegeAidItem(BaseModel):
    college_id: uuid.UUID
    name: str
    state: str
    region: str
    control: Literal["public", "private"]
    cds_year: str | None  # null = the source did not report a year
    release_label: str
    source: str | None
    coa_in_state: JsonDecimal | None
    coa_out_of_state: JsonDecimal | None
    coa_year: str | None
    coa_estimated: bool
    full_time_undergrads: int | None
    aid_application: str | None
    aid_form: AidForm | None
    pct_need_met: JsonDecimal | None
    pct_non_need_receiving_merit: JsonDecimal | None
    avg_merit_award: JsonDecimal | None
    # Derived at read time ("computed by CMM").
    pct_need_unmet: JsonDecimal | None
    pct_non_need_paying_full_coa: JsonDecimal | None
    merit_pct_of_coa_out_of_state: JsonDecimal | None
    merit_pct_of_coa_in_state: JsonDecimal | None


class CollegeAidListOut(BaseModel):
    items: list[CollegeAidItem]
    generated_at: datetime
    latest_release_label: str | None
    max_cds_year: str | None
    # Admin-set "Find your SAI" link (Global Settings); None = no link.
    sai_calculator_url: str | None = None
