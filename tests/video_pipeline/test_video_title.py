"""The name a published replay carries on Vimeo.

The library is browsed by people looking for a workshop, on a date, for a
cohort — not by people who remember how the operations team worded the calendar
invite. So the title is assembled from the workshop rather than taken from
``webinar_name``, and the two cases that matter are the pieces that are missing
and the date, which is stored in UTC and read by people in Eastern time.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from src.cycles.models import Cohort
from src.video_pipeline import job_service
from src.video_pipeline.video_title import MAX_TITLE, video_title
from src.workshops.models import Webinar, Workshop


@pytest.fixture
def full(db):
    """A webinar with every part of the title present."""
    workshop = Workshop(
        id=uuid.uuid4(),
        name="Navigating the New System of College Pricing and Financial Aid",
        sequence_number=1,
    )
    cohort = Cohort(id=uuid.uuid4(), name=f"MOUNT-{uuid.uuid4().hex[:6]}")
    webinar = Webinar(
        id=uuid.uuid4(),
        workshop_id=workshop.id,
        cohort_id=cohort.id,
        webinar_name="Nov 17 sitting — do not use this wording",
        zoom_webinar_id=f"z{uuid.uuid4().hex[:10]}",
        # 7pm Eastern on 17 November, as it is stored: the next day in UTC.
        start_datetime=datetime(2025, 11, 18, 0, 0, tzinfo=timezone.utc),
    )
    db.add_all([workshop, cohort, webinar])
    db.commit()
    return webinar


def _job(db, webinar=None, *, audit_only=False):
    job, _ = job_service.create_from_recording(
        db,
        zoom_recording_uuid=f"rec-{uuid.uuid4()}",
        webinar_id=webinar.id if webinar else None,
        audit_only=audit_only,
    )
    return job


def test_the_title_is_built_from_the_workshop_not_the_webinar(db, full):
    """``webinar_name`` says which sitting this was, in whoever's wording. The
    library is browsed by workshop, date and cohort."""
    assert video_title(_job(db, full)) == (
        "Workshop #1 Navigating the New System of College Pricing and Financial Aid "
        f"November 17 2025 {full.cohort.name} Schools"
    )


def test_the_date_is_the_one_the_workshop_was_advertised_on(db, full):
    """A 7pm Eastern session is stored as the next day in UTC. Formatting the
    stored value would date a third of the library one day late."""
    assert "November 17 2025" in video_title(_job(db, full))


def test_a_missing_piece_is_left_out_rather_than_filled_in(db):
    workshop = Workshop(id=uuid.uuid4(), name="Paying for College")
    webinar = Webinar(
        id=uuid.uuid4(),
        workshop_id=workshop.id,
        webinar_name="Sept sitting",
        zoom_webinar_id=f"z{uuid.uuid4().hex[:10]}",
    )
    db.add_all([workshop, webinar])
    db.commit()

    assert video_title(_job(db, webinar)) == "Paying for College"


def test_a_run_with_no_webinar_keeps_the_fallback_it_always_had(db):
    job = _job(db)

    assert video_title(job) == f"Webinar replay {job.id}"


def test_an_audit_run_is_labelled_and_still_fits_the_limit(db):
    workshop = Workshop(id=uuid.uuid4(), name="W" * 200, sequence_number=9)
    webinar = Webinar(
        id=uuid.uuid4(),
        workshop_id=workshop.id,
        zoom_webinar_id=f"z{uuid.uuid4().hex[:10]}",
    )
    db.add_all([workshop, webinar])
    db.commit()

    title = video_title(_job(db, webinar, audit_only=True))

    assert title.startswith("[Audit] Workshop #9 ")
    assert len(title) <= MAX_TITLE
