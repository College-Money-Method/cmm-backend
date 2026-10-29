"""College Aid Explorer public API: password -> token, then the latest-aid set.

Standalone and password gated (not tied to SRC schools). The password lives in
Global Settings (app_config); see ``access_token`` for the token rules.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Response

from src.colleges.access_password import verify_password
from src.colleges.access_token import (
    CollegeAidAccessDep,
    current_password_hash,
    issue_access_token,
)
from src.colleges.derived_metrics import to_public_item
from src.colleges.estimate import CollegeAidFacts, EstimateInputs, estimate_for_college
from src.colleges.estimate_schemas import (
    EstimateInputsOut,
    EstimateItem,
    EstimateListOut,
    EstimateQueryIn,
)
from src.colleges.latest_query import fetch_latest_records
from src.colleges.public_schemas import (
    CollegeAidAccessIn,
    CollegeAidAccessOut,
    CollegeAidListOut,
)
from src.db.deps import DbDep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/colleges", tags=["colleges"])

# Fixed pause before a wrong-password answer: a cheap brute-force brake.
# Module-level so tests can zero it.
BAD_PASSWORD_DELAY_SECONDS = 1.0


@router.post("/access", response_model=CollegeAidAccessOut)
def request_access(body: CollegeAidAccessIn, db: DbDep) -> CollegeAidAccessOut:
    """Exchange the shared password for a 7-day access token."""
    stored = current_password_hash(db)
    if not stored:
        raise HTTPException(status_code=403, detail="college_aid_disabled")
    if not verify_password(body.password, stored):
        time.sleep(BAD_PASSWORD_DELAY_SECONDS)
        logger.warning("college aid access: wrong password")
        raise HTTPException(status_code=401, detail="college_aid_bad_password")
    token, expires_at = issue_access_token(stored)
    return CollegeAidAccessOut(token=token, expires_at=expires_at)


@router.get("/public", response_model=CollegeAidListOut, dependencies=[CollegeAidAccessDep])
def list_public(db: DbDep, response: Response) -> CollegeAidListOut:
    """Latest aid record for every visible college, with derived metrics."""
    response.headers["Cache-Control"] = "private, no-store"
    pairs = fetch_latest_records(db)
    years = [r.cds_year for _, r in pairs if r.cds_year]
    releases = [r.release_label for _, r in pairs]
    return CollegeAidListOut(
        items=[to_public_item(c, r) for c, r in pairs],
        generated_at=datetime.now(UTC),
        latest_release_label=max(releases, default=None),
        max_cds_year=max(years, default=None),
    )


@router.post("/public/estimates", response_model=EstimateListOut, dependencies=[CollegeAidAccessDep])
def list_estimates(body: EstimateQueryIn, db: DbDep, response: Response) -> EstimateListOut:
    """Per-college aid estimate for the family's own numbers.

    The inputs are family financial data: they arrive in the POST body (so they
    stay out of server and load balancer access logs), are used for this
    response only, and are never stored or logged.
    """
    response.headers["Cache-Control"] = "private, no-store"
    sai, merit = body.sai, body.merit
    state = body.home_state.upper() if body.home_state else None
    mode = merit if merit in ("none", "avg") else "amount"
    amount = Decimal(merit) if mode == "amount" else None
    inputs = EstimateInputs(sai=sai, home_state=state, merit_mode=mode, merit_amount=amount)
    items = []
    for college, record in fetch_latest_records(db):
        facts = CollegeAidFacts(
            control=college.control, state=college.state, coa_in_state=record.coa_in_state,
            coa_out_of_state=record.coa_out_of_state, coa_year=record.coa_year,
            pct_need_met=record.pct_need_met, avg_merit_award=record.avg_merit_award,
        )
        result = estimate_for_college(facts, inputs)
        items.append(EstimateItem(college_id=college.id, **vars(result)))
    return EstimateListOut(
        inputs=EstimateInputsOut(sai=sai, home_state=state, merit_mode=mode, merit_amount=amount),
        items=items,
    )
