"""Timing checks: was evidence public before move start? Excludes missing/ambiguous timestamps (gate 5)."""

from datetime import timedelta

from backend import config
from backend.models import Evidence, Move, RejectedEvidence

MISSING_TIMESTAMP = "missing timestamp"
AMBIGUOUS_TIMESTAMP = "ambiguous timestamp (date only, on the day of the move)"


def describe_gap(delta: timedelta) -> str:
    """Turn a positive timedelta into e.g. '14 minutes' or '2 hours 5 minutes'."""
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes < 1:
        return "less than a minute"
    days, rem = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(rem, 60)
    parts = []
    for value, unit in ((days, "day"), (hours, "hour"), (minutes, "minute")):
        if value:
            parts.append(f"{value} {unit}{'' if value == 1 else 's'}")
    return " ".join(parts[:2])


def timestamp_problem(evidence: Evidence, move: Move) -> str | None:
    """Return why an evidence timestamp is unusable (gate 5), or None if it is fine."""
    t = evidence.first_public_at
    if t is None:
        return MISSING_TIMESTAMP
    if (
        config.MIDNIGHT_ON_MOVE_DAY_IS_AMBIGUOUS
        and (t.hour, t.minute, t.second, t.microsecond) == (0, 0, 0, 0)
        and t.date() == move.move_start_at.date()
    ):
        return AMBIGUOUS_TIMESTAMP
    return None


def split_by_timing(
    move: Move, evidence: list[Evidence]
) -> tuple[list[Evidence], list[RejectedEvidence]]:
    """Accept evidence public strictly before move start; reject the rest with a reason."""
    accepted: list[Evidence] = []
    rejected: list[RejectedEvidence] = []
    for item in evidence:
        problem = timestamp_problem(item, move)
        if problem:
            rejected.append(RejectedEvidence(evidence=item, reason=problem))
            continue
        gap = item.first_public_at - move.move_start_at
        if gap < timedelta(0):
            accepted.append(item)
        elif gap == timedelta(0):
            rejected.append(RejectedEvidence(
                evidence=item, reason="became public exactly when the move began, not before it"))
        else:
            rejected.append(RejectedEvidence(
                evidence=item, reason=f"became public {describe_gap(gap)} after the move began"))
    return accepted, rejected
