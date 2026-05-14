"""Source adapters: GitHub (PRs), Jira (issues), … all returning WorkItem.

A Source fetches completed items from some upstream system in a date
window and converts them to source-agnostic `WorkItem` objects so the
rest of the pipeline (clustering, compute, forecast, renderers) doesn't
know what produced them.
"""

from __future__ import annotations

from datetime import date
from typing import Protocol

from ..compute import WorkItem


class Source(Protocol):
    """Anything that can produce `WorkItem`s for completed and in-flight items."""

    def fetch_completed_in_window(self, start: date, stop: date) -> list[WorkItem]:
        ...

    def fetch_for_percentile_training(
        self, start: date, stop: date
    ) -> list[WorkItem]:
        """Lightweight variant of `fetch_completed_in_window` for
        callers that only need ``cycle_time`` (Aging's percentile-line
        subroutine). Sources may return WorkItems without `activity`
        events populated. The flow-efficiency path must continue to use
        the full `fetch_completed_in_window`.
        """
        ...

    def fetch_in_flight(self, asof: date) -> list[WorkItem]:
        """Items that have entered but not yet exited the workflow as of `asof`.

        Returned WorkItems have ``merged_at=None``. Their final
        ``status_intervals`` entry runs through `asof` so the renderer can
        read the current workflow state directly from
        ``status_intervals[-1].status``.
        """
        ...

    @property
    def label(self) -> str:
        """Short human-readable identifier (e.g. ``astral-sh/uv`` or ``ASF/BIGTOP``)."""
        ...
