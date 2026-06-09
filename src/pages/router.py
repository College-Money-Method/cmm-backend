"""CMS pages API router."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from src.auth.deps import AdminDep
from src.db.deps import DbDep
from src.pages.models import Page
from src.pages.schemas import PageCreate, PageListItem, PageListResponse, PageOut, PageUpdate

router = APIRouter(prefix="/api/v1/pages", tags=["pages"])


@router.get("", response_model=PageListResponse)
def list_pages(_admin: AdminDep, db: DbDep):
    """Admin: list all pages."""
    items = db.scalars(select(Page).order_by(Page.title)).all()
    total = db.scalar(select(func.count()).select_from(Page)) or 0
    return PageListResponse(items=list(items), total=total)


@router.get("/public/{slug}", response_model=PageOut)
def get_page_by_slug_public(slug: str, db: DbDep):
    """Public: get a published page by slug — no auth required."""
    page = db.scalar(select(Page).where(Page.slug == slug, Page.status == "published"))
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


@router.get("/{page_id}", response_model=PageOut)
def get_page(_admin: AdminDep, page_id: uuid.UUID, db: DbDep):
    """Admin: get a page by id."""
    page = db.get(Page, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


@router.post("", response_model=PageOut, status_code=status.HTTP_201_CREATED)
def create_page(body: PageCreate, _admin: AdminDep, db: DbDep):
    """Admin: create a new page."""
    existing = db.scalar(select(Page).where(Page.slug == body.slug))
    if existing:
        raise HTTPException(status_code=409, detail="Page with this slug already exists")
    page = Page(**body.model_dump())
    db.add(page)
    db.commit()
    db.refresh(page)
    return page


@router.patch("/{page_id}", response_model=PageOut)
def update_page(page_id: uuid.UUID, body: PageUpdate, _admin: AdminDep, db: DbDep):
    """Admin: update a page."""
    page = db.get(Page, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    data = body.model_dump(exclude_unset=True)
    if "slug" in data and data["slug"] != page.slug:
        conflict = db.scalar(select(Page).where(Page.slug == data["slug"]))
        if conflict:
            raise HTTPException(status_code=409, detail="Page with this slug already exists")
    for k, v in data.items():
        setattr(page, k, v)
    page.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(page)
    return page


@router.delete("/{page_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_page(page_id: uuid.UUID, _admin: AdminDep, db: DbDep):
    """Admin: delete a page."""
    page = db.get(Page, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    db.delete(page)
    db.commit()
