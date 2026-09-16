"""Job states and the transitions the pipeline is allowed to make between them.

The job row is the single source of truth for pipeline position, so an illegal
write is a bug that must surface at the write, not three stages later as output
that looks plausible. ``assert_legal`` is the guard; ``job_service.advance`` is
the only place it is called.

Ownership across the state machine:

    pending ──► processing ──► chaptering ──► published ──┐
       ▲    │        │  │           │                      │ (force retry)
       │    └────────┴──┴───────────┴───────► failed ──► pending  (retry)
       └─────────────┘  (requeue: the source was not ready yet)

``pending`` is written by intake (webhook or reconcile). ``processing`` is
written at ECS dispatch and owned by the task for its duration — the transition
is made by the dispatcher rather than the task itself so the concurrency count,
which reads ``processing``, cannot miss a task that has been launched but has
not booted yet. ``chaptering`` and ``published`` are owned by the API.

``failed`` is reachable from every active state and leaves
``webinars.video_embed_code`` untouched, so a failed run degrades to "no replay
yet" rather than to a broken one.

``published -> pending`` exists for the admin screen's force retry and nothing
else. A published job is finished as far as the pipeline is concerned, so no
automatic path leads back out of it; an operator who can see the published
chapters are wrong is the only thing that makes re-running worthwhile, and the
re-run reuses the Vimeo video it already has rather than publishing a second
one. ``router.retry_job`` is what gates it.

``processing -> pending`` is the one way back. A task that finds the source not
yet available has done no work and holds a slot it cannot use, so it gives the
slot back and lets the sweeper try again later. It is deliberately not a
general-purpose escape hatch: a task that has started processing must go on to
`chaptering` or `failed`, because by then there is output to account for.
"""

from __future__ import annotations

from enum import Enum


class JobState(str, Enum):
    """Position of a job in the pipeline. Values are what is stored in the DB."""

    PENDING = "pending"
    PROCESSING = "processing"
    CHAPTERING = "chaptering"
    PUBLISHED = "published"
    FAILED = "failed"


# States a job can still move out of on its own — what the sweeper and the
# admin screen treat as "in flight" rather than settled.
ACTIVE_STATES: frozenset[JobState] = frozenset(
    {JobState.PENDING, JobState.PROCESSING, JobState.CHAPTERING}
)

# Where a job comes to rest on its own. `failed` is not terminal either — retry
# re-arms it — and `published` is only terminal until an operator forces a
# re-run, which no part of the pipeline does by itself.
TERMINAL_STATES: frozenset[JobState] = frozenset({JobState.PUBLISHED})

LEGAL_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.PENDING: frozenset({JobState.PROCESSING, JobState.FAILED}),
    JobState.PROCESSING: frozenset({JobState.CHAPTERING, JobState.FAILED, JobState.PENDING}),
    JobState.CHAPTERING: frozenset({JobState.PUBLISHED, JobState.FAILED}),
    # Only a force retry, driven by an operator, takes this edge.
    JobState.PUBLISHED: frozenset({JobState.PENDING}),
    # Retry is the only way out of `failed`, and it goes back to the start.
    JobState.FAILED: frozenset({JobState.PENDING}),
}


class IllegalTransition(ValueError):
    """Raised when a caller tries to move a job somewhere it cannot go."""


def is_legal(source: JobState, target: JobState) -> bool:
    """True when ``source -> target`` is a transition the pipeline defines."""
    return target in LEGAL_TRANSITIONS.get(source, frozenset())


def assert_legal(source: JobState, target: JobState) -> None:
    """Raise :class:`IllegalTransition` unless ``source -> target`` is allowed."""
    if not is_legal(source, target):
        raise IllegalTransition(
            f"illegal video job transition {source.value} -> {target.value}"
        )
