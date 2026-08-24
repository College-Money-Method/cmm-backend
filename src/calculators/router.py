"""Embeddable calculators API router.

Storage only — calculation happens in the authored HTML/JS, in the browser.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from src.auth.deps import AdminDep
from src.calculators.models import Calculator
from src.calculators.schemas import (
    CalculatorCreate,
    CalculatorListItem,
    CalculatorListResponse,
    CalculatorOut,
    CalculatorSlugOut,
    CalculatorUpdate,
)
from src.db.deps import DbDep

router = APIRouter(prefix="/api/v1/calculators", tags=["calculators"])

_NOT_FOUND = "Calculator not found"
_SLUG_CONFLICT = "Calculator with this slug already exists"


@router.get("", response_model=CalculatorListResponse)
def list_calculators(_admin: AdminDep, db: DbDep):
    """Admin: list all calculators."""
    items = db.scalars(select(Calculator).order_by(Calculator.title)).all()
    total = db.scalar(select(func.count()).select_from(Calculator)) or 0
    return CalculatorListResponse(items=list(items), total=total)


# The /public routes are declared before /{calculator_id} so that "public" is
# never parsed as a UUID path parameter.
@router.get("/public", response_model=list[CalculatorSlugOut])
def list_published_calculator_slugs(db: DbDep):
    """Public: published slugs, for sitemap generation and the embed picker."""
    rows = db.execute(
        select(Calculator.slug, Calculator.title, Calculator.updated_at)
        .where(Calculator.status == "published")
        .order_by(Calculator.slug)
    ).all()
    return [
        CalculatorSlugOut(slug=slug, title=title, updated_at=updated_at)
        for slug, title, updated_at in rows
    ]


@router.get("/public/{slug}", response_model=CalculatorOut)
def get_calculator_by_slug_public(slug: str, db: DbDep):
    """Public: a published calculator by slug — no auth required.

    Drafts must stay unreachable here: an unpublished calculator can hold a
    half-finished formula, and a slug is guessable.
    """
    calc = db.scalar(
        select(Calculator).where(Calculator.slug == slug, Calculator.status == "published")
    )
    if not calc:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    out = CalculatorOut.model_validate(calc)
    # `documentation` is internal authoring commentary — which divergences from a
    # source workbook were deliberate, what is still unconfirmed. Nothing renders
    # it, so withholding it costs the embed nothing and keeps those notes off an
    # unauthenticated, publicly cacheable response.
    out.documentation = None
    return out


@router.get("/{calculator_id}", response_model=CalculatorOut)
def get_calculator(_admin: AdminDep, calculator_id: uuid.UUID, db: DbDep):
    """Admin: get a calculator by id."""
    calc = db.get(Calculator, calculator_id)
    if not calc:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    return calc


@router.post("", response_model=CalculatorOut, status_code=status.HTTP_201_CREATED)
def create_calculator(body: CalculatorCreate, _admin: AdminDep, db: DbDep):
    """Admin: create a calculator."""
    if db.scalar(select(Calculator).where(Calculator.slug == body.slug)):
        raise HTTPException(status_code=409, detail=_SLUG_CONFLICT)
    calc = Calculator(**body.model_dump())
    db.add(calc)
    db.commit()
    db.refresh(calc)
    return calc


@router.patch("/{calculator_id}", response_model=CalculatorOut)
def update_calculator(
    calculator_id: uuid.UUID, body: CalculatorUpdate, _admin: AdminDep, db: DbDep
):
    """Admin: update a calculator."""
    calc = db.get(Calculator, calculator_id)
    if not calc:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    data = body.model_dump(exclude_unset=True)
    if "slug" in data and data["slug"] != calc.slug:
        if db.scalar(select(Calculator).where(Calculator.slug == data["slug"])):
            raise HTTPException(status_code=409, detail=_SLUG_CONFLICT)
    for k, v in data.items():
        setattr(calc, k, v)
    calc.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(calc)
    return calc


@router.delete("/{calculator_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_calculator(_admin: AdminDep, calculator_id: uuid.UUID, db: DbDep):
    """Admin: delete a calculator."""
    calc = db.get(Calculator, calculator_id)
    if not calc:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    db.delete(calc)
    db.commit()
