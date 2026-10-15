"""Public endpoints: open slots, taking a request, and Paul's token-gated review link.

The review routes are reachable by anyone holding the emailed link, so every
bad token (malformed, forged, unknown request, mismatched expiry) answers the
same 404, and acting only ever happens on POST: mail scanners pre-fetch links.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse

from src.auth.rate_limit import allow, client_ip
from src.db.deps import DbDep
from src.integrations.turnstile import verify_turnstile
from src.paul_booking import decisions, service
from src.paul_booking.holds import SlotTaken
from src.paul_booking.models import PaulMeetingRequest
from src.paul_booking.review_token import expiry_epoch, parse_review_token
from src.paul_booking.rules import DURATIONS
from src.paul_booking.schemas import (
    DeclineIn,
    RequestCreate,
    RequestOut,
    RequestReceipt,
    ReviewOut,
    SlotsOut,
)

router = APIRouter(prefix="/api/v1/paul-booking", tags=["paul-booking"])

# Same budget as the contact form: a few asks, then a pause.
_SUBMIT_LIMIT = 3
_SUBMIT_WINDOW_SECONDS = 600.0
# The slot picker loads both lengths and reloads after a lost race; this is
# well above that, and caps how hard one address can make us ask Zoom.
_SLOTS_LIMIT = 30
_SLOTS_WINDOW_SECONDS = 60.0
_INVALID_LINK = "This link is invalid."
# Must match the ``action`` the frontend's Turnstile widget is rendered with.
CAPTCHA_ACTION = "paul_booking"
_PRIVATE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _unavailable() -> HTTPException:
    return HTTPException(status_code=503, detail="Booking is not available right now.")


def decision_error_response(exc: decisions.DecisionError) -> JSONResponse:
    """409 carries the current status as ``status``; every error also has ``detail``."""
    body = {"detail": exc.detail} if not isinstance(exc.detail, dict) else {"detail": "Already decided", **exc.detail}
    return JSONResponse(status_code=exc.status_code, content=body, headers=_PRIVATE_HEADERS)


@router.get("/slots", response_model=SlotsOut)
def get_slots(request: Request, db: DbDep, duration: int = Query(...)):
    """Open start times for a call length, within the notice and horizon."""
    if duration not in DURATIONS:  # Literal ints do not coerce from query strings
        raise HTTPException(status_code=422, detail="duration must be 30 or 60")
    if not allow(f"paul-booking-slots:{client_ip(request)}", limit=_SLOTS_LIMIT, window_seconds=_SLOTS_WINDOW_SECONDS):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a minute and try again.")
    try:
        cfg = service.require_enabled(db)
        slots = service.open_slots(db, cfg, duration, _now())
    except service.BookingUnavailable as exc:
        raise _unavailable() from exc
    return SlotsOut(host_timezone=cfg.tz_name, duration=duration, slots=slots)


@router.post("/requests", response_model=RequestReceipt, status_code=status.HTTP_202_ACCEPTED)
def create_request(body: RequestCreate, request: Request, db: DbDep):
    """Hold a slot and ask Paul to confirm it.

    400 the CAPTCHA did not pass, 409 the time was just taken, 422 not an
    offerable slot, 429 rate limited or too many open requests, 503 booking off
    or Zoom unreachable.
    """
    try:
        cfg = service.require_enabled(db)
    except service.BookingUnavailable as exc:
        raise _unavailable() from exc
    if not allow(f"paul-booking:{client_ip(request)}", limit=_SUBMIT_LIMIT, window_seconds=_SUBMIT_WINDOW_SECONDS):
        raise HTTPException(status_code=429, detail="Too many requests. Please wait a few minutes and try again.")
    # After the rate limit, so a flood is cut off before it costs a call to Cloudflare.
    if not verify_turnstile(body.captcha_token, client_ip(request), action=CAPTCHA_ACTION):
        raise HTTPException(status_code=400, detail="We couldn't verify you're not a robot. Please try again.")
    try:
        return service.create_request(db, cfg, body, _now())
    except SlotTaken as exc:
        raise HTTPException(status_code=409, detail="That time was just taken. Please pick another.") from exc
    except service.InvalidSlot as exc:
        raise HTTPException(status_code=422, detail="That time is not available.") from exc
    except service.TooManyPending as exc:
        raise HTTPException(status_code=429, detail="You already have open requests. Please wait for Paul's reply.") from exc
    except service.BookingUnavailable as exc:
        raise _unavailable() from exc


def _request_for_token(db: DbDep, token: str, *, acting: bool) -> PaulMeetingRequest:
    """The request a review token points at; HTTP 404 (bad token) or 410 (expired).

    Only acting is refused once the link expires, and only while the request is
    undecided. Viewing an expired link still works, so Paul sees when it lapsed
    (``lapsed: true``) or what he decided (and the join link). Acting on a
    decided request is refused by the decision service with a 409.
    """
    parsed = parse_review_token(token)
    req = db.get(PaulMeetingRequest, parsed[0]) if parsed else None
    if parsed is None or req is None or parsed[1] != expiry_epoch(req.expires_at):
        raise HTTPException(status_code=404, detail=_INVALID_LINK, headers=_PRIVATE_HEADERS)
    if acting and req.status in ("pending", "approving") and time.time() >= parsed[1]:
        raise HTTPException(status_code=410, detail="This request has lapsed.", headers=_PRIVATE_HEADERS)
    return req


@router.get("/review/{token}", response_model=ReviewOut)
def get_review(token: str, response: Response, db: DbDep):
    response.headers.update(_PRIVATE_HEADERS)
    req = _request_for_token(db, token, acting=False)
    now = _now()
    out = service.request_out(req, now)
    return ReviewOut(**out.model_dump(), host_timezone=service.load_settings(db).tz_name)


@router.post("/review/{token}/approve", response_model=RequestOut)
def approve_by_token(token: str, response: Response, db: DbDep):
    response.headers.update(_PRIVATE_HEADERS)
    request_id: uuid.UUID = _request_for_token(db, token, acting=True).id
    now = _now()
    try:
        return service.request_out(decisions.approve(db, request_id, now), now)
    except decisions.DecisionError as exc:
        return decision_error_response(exc)


@router.post("/review/{token}/decline", response_model=RequestOut)
def decline_by_token(token: str, response: Response, db: DbDep, body: DeclineIn | None = None):
    response.headers.update(_PRIVATE_HEADERS)
    request_id: uuid.UUID = _request_for_token(db, token, acting=True).id
    now = _now()
    try:
        return service.request_out(decisions.decline(db, request_id, body.note if body else None, now), now)
    except decisions.DecisionError as exc:
        return decision_error_response(exc)
