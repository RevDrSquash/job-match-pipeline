"""Atomic quota helpers shared by screen-job and the generate API."""

from __future__ import annotations

import uuid

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.db.models import User


def try_consume_quota(session: Session, user_id: uuid.UUID) -> bool:
    """Decrement ``users.quota_remaining`` by 1 when it is positive.

    Returns True only when a row was updated. ``quota_remaining IS NULL``
    and ``0`` both fail closed (no generation).
    """
    result = session.execute(
        update(User)
        .where(
            User.id == user_id,
            User.quota_remaining.is_not(None),
            User.quota_remaining > 0,
        )
        .values(quota_remaining=User.quota_remaining - 1)
    )
    return result.rowcount == 1
