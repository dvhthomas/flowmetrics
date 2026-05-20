"""Two user-facing date-range concepts for the dashboard:

- **View window** — clamps chart x-axes. Display-only; doesn't
  change what feeds the math.
- **Reference period** — the statistical sample. Drives
  percentile thresholds (cycle-time, aging) and the Monte Carlo
  throughput sampling distribution (forecast).

Both windows are inclusive on both endpoints — the UI labels
("From" / "To") match. Same-day is a 1-day window, not 0.

URL state:

    ?view_from=YYYY-MM-DD&view_to=YYYY-MM-DD
    &ref_from=YYYY-MM-DD&ref_to=YYYY-MM-DD

Missing or invalid params → fall back to defaults anchored to
today UTC: 30-day view, 14-day reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


DEFAULT_VIEW_DAYS = 30
DEFAULT_REFERENCE_DAYS = 14


@dataclass(frozen=True)
class Window:
    """An inclusive date range [from_, to]. Same-day is 1 day."""

    from_: date
    to: date

    @property
    def days_inclusive(self) -> int:
        return (self.to - self.from_).days + 1

    @classmethod
    def last_n_days(cls, n: int, *, today: date) -> "Window":
        """Window of `n` inclusive days ending on `today`."""
        return cls(from_=today - timedelta(days=n - 1), to=today)


def last_completed_week(today: date) -> Window:
    """The most recent COMPLETED Sunday-to-Saturday week.

    The current week the viewer is in is excluded — "last week"
    means "the week that just finished", not "rolling 7 days".
    If today IS Sunday, the just-finished week is the answer.
    """
    # Python: weekday() returns 0=Monday..6=Sunday.
    # Distance from today back to the most recent Saturday (the
    # END of the most recent completed week). When today is
    # Sunday, that's yesterday. When today is Saturday, today
    # is mid-week, so the most recent completed Saturday is 7
    # days ago.
    days_back_to_sat = (today.weekday() - 5) % 7
    if days_back_to_sat == 0:
        days_back_to_sat = 7  # today is Sat → previous Sat
    last_sat = today - timedelta(days=days_back_to_sat)
    return Window(
        from_=last_sat - timedelta(days=6),  # the Sunday before
        to=last_sat,
    )


def parse_windows(
    query: dict[str, str] | dict, today: date
) -> tuple[Window, Window]:
    """Parse window params from a query dict. Returns
    `(view_window, reference_period)`. Two URL shapes are
    supported:

      1. **Common case** (preset / anchor + durations):
         `?anchor=YYYY-MM-DD&view_days=N&ref_days=M`
         Derives both windows as N (and M) inclusive days
         ending on the anchor. Missing days fall back to
         DEFAULT_VIEW_DAYS / DEFAULT_REFERENCE_DAYS. Missing
         anchor falls back to today UTC.

      2. **Advanced / legacy** (explicit dates):
         `?view_from=YYYY-MM-DD&view_to=YYYY-MM-DD` (and
         `ref_from`/`ref_to`). Used by the advanced controls
         and existing shared links.

    Precedence per window: explicit from+to wins over
    anchor+days. Never raises on user input.
    """
    # Resolve the anchor first — common to both windows when
    # advanced from/to aren't supplied.
    anchor_str = query.get("anchor")
    if anchor_str:
        try:
            anchor = date.fromisoformat(str(anchor_str))
        except ValueError:
            anchor = today
    else:
        anchor = today

    def _parse(prefix: str, default_days: int) -> Window:
        # Advanced wins first.
        from_str = query.get(f"{prefix}_from")
        to_str = query.get(f"{prefix}_to")
        if from_str and to_str:
            try:
                return Window(
                    from_=date.fromisoformat(str(from_str)),
                    to=date.fromisoformat(str(to_str)),
                )
            except ValueError:
                pass  # fall through to anchor+days
        # Anchor + days (common case).
        days_str = query.get(f"{prefix}_days")
        if days_str:
            try:
                days = int(days_str)
                if days > 0:
                    return Window(
                        from_=anchor - timedelta(days=days - 1),
                        to=anchor,
                    )
            except ValueError:
                pass
        # Defaults — anchored at the resolved anchor.
        return Window.last_n_days(default_days, today=anchor)

    return (
        _parse("view", DEFAULT_VIEW_DAYS),
        _parse("ref", DEFAULT_REFERENCE_DAYS),
    )
