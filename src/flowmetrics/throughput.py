from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from .compute import WorkItem


def daily_throughput(
    prs: Iterable[WorkItem],
    start: date,
    stop: date,
) -> list[int]:
    """Daily merge counts across `[start, stop]` (inclusive).

    Zero-merge days are included as zero — they are real historical
    observations and the Monte Carlo sampler needs them to represent
    "slow days" in the distribution.
    """
    if stop < start:
        raise ValueError(f"stop ({stop}) must be >= start ({start})")

    span = (stop - start).days + 1
    counts = [0] * span
    for pr in prs:
        if pr.merged_at is None:
            continue
        day = pr.merged_at.date()
        if start <= day <= stop:
            counts[(day - start).days] += 1
    return counts
