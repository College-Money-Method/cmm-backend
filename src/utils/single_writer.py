"""Elects one process to run a scheduled job that must not run twice.

Prod serves the API with two uvicorn workers (``WEB_CONCURRENCY=2``), and each
worker runs the app's lifespan, so each one starts its own APScheduler with its
own copy of the sweep. APScheduler's ``max_instances`` only stops a job
overlapping *itself within one scheduler*, so the two copies fire together —
observed 52 ms apart — and both do the whole of the same job.

That is not merely wasteful. Two chaptering passes over one Vimeo video both
tried to replace its chapter list: the first won, the second's chapter POSTs
collided with the chapters the first had just created, and Vimeo refused them
with "the timecode must be unique". The result was a job marked failed on top of
a replay that had in fact published correctly.

An OS file lock is the whole mechanism. The workers are processes in one
container, so a lock file they share is enough to pick one of them, and it needs
no database round-trip, no schema, and no coordination service. The winner keeps
the descriptor open for the life of the process; the losers ask again on every
tick, so if the leader dies its lock dies with it and a survivor takes over on
the next interval rather than the pipeline going quiet until a redeploy.

The video pipeline's sweep is the job that made this necessary; the email
automations check has the same shape, reading a ledger of what it has already
sent and then sending, so two copies of it a few milliseconds apart can both
decide the same email is still owed.

Scope: this elects one process *per container*. It is the right guard for the
current shape — one backend task, several workers — and would need a shared lock
(a Postgres advisory lock, say) if the service is ever scaled to more than one
task.
"""

from __future__ import annotations

import fcntl
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# Descriptors held for the life of the process, one per lock name. Keeping them
# here is not bookkeeping — closing the file releases the lock, so dropping the
# reference would hand leadership away at the next garbage collection.
_held: dict[str, int] = {}


def _lock_path(name: str) -> str:
    return os.path.join(tempfile.gettempdir(), f"cmm-{name}.lock")


def is_leader(name: str) -> bool:
    """True when this process holds (or has just taken) the lock called ``name``.

    Non-blocking on purpose: a worker that is not the leader should skip its tick
    and come back at the next interval, not queue up behind the one that is.
    """
    if name in _held:
        return True

    fd = os.open(_lock_path(name), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False

    _held[name] = fd
    logger.info("This worker is now the leader for %s (pid=%d)", name, os.getpid())
    return True
