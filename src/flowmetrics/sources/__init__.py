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
    """Anything that can produce a list of completed `WorkItem`s for a window."""

    def fetch_completed_in_window(self, start: date, stop: date) -> list[WorkItem]:
        ...

    @property
    def label(self) -> str:
        """Short human-readable identifier (e.g. ``astral-sh/uv`` or ``ASF/BIGTOP``)."""
        ...
