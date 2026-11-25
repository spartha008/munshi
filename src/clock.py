"""One place for "now". Always India time (servers run in UTC); no DST in India.

Tests and scripts can pin it with set_now() or MUNSHI_NOW=2026-11-25T10:00.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))
_override: datetime | None = None


def now() -> datetime:
    """Naive datetime in IST."""
    if _override is not None:
        return _override
    env = os.environ.get("MUNSHI_NOW")
    if env:
        return datetime.fromisoformat(env)
    return datetime.now(IST).replace(tzinfo=None, second=0, microsecond=0)


def today() -> date:
    return now().date()


def set_now(dt: datetime | None) -> None:
    global _override
    _override = dt
