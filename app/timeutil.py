"""Time helpers.

Convention: every timestamp FlowOps stores or compares is NAIVE UTC. Produce "now" with
utcnow(); never call datetime.utcnow() (deprecated, and naive by accident). Convert to
local time for display only.
"""
from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    """Current time as a naive UTC datetime."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_naive_utc(value: datetime | None) -> datetime | None:
    """Convert an aware datetime to naive UTC; a naive one is assumed to be UTC already."""
    if value is None:
        return None
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def compute_duration_ms(
    started_at: datetime | None, finished_at: datetime | None
) -> int | None:
    """Milliseconds between the two timestamps, or None when it cannot be known.

    None (never 0) when either timestamp is missing, and also when finished_at is before
    started_at (clock skew) -- a negative duration is not a real measurement.
    """
    if started_at is None or finished_at is None:
        return None
    delta = to_naive_utc(finished_at) - to_naive_utc(started_at)
    if delta < timedelta(0):
        return None
    return delta // timedelta(milliseconds=1)
