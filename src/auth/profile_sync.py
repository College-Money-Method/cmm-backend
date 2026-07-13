"""Keep the local ``profiles`` table in sync with Supabase ``auth.users``.

Callers pass the authoritative email/name (usually straight from a Supabase
response) and commit their own transaction — these helpers only stage the
change.
"""

import uuid

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from src.auth.models import Profile


def upsert_profile(
    db: Session,
    user_id: uuid.UUID | str,
    email: str,
    first_name: str | None = None,
    last_name: str | None = None,
) -> None:
    """Insert or update the profile row for ``user_id`` (does not commit)."""
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    values = {
        "user_id": uid,
        "email": email or "",
        "first_name": first_name or None,
        "last_name": last_name or None,
    }
    stmt = pg_insert(Profile).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Profile.user_id],
        set_={
            "email": stmt.excluded.email,
            "first_name": stmt.excluded.first_name,
            "last_name": stmt.excluded.last_name,
        },
    )
    db.execute(stmt)


def delete_profile(db: Session, user_id: uuid.UUID | str) -> None:
    """Remove the profile row for ``user_id`` (does not commit)."""
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    db.query(Profile).filter(Profile.user_id == uid).delete()
