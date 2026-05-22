"""Two date-range concepts for the dashboard:

- **View window** — navigation. What date range the charts
  show; anchored to the user's chosen anchor (today by default).
- **Reference period** — the statistical sample. Drives
  percentile thresholds (aging) and the Monte Carlo throughput
  distribution (forecast). Anchored to the most recent data
  (`data_max`), NOT the view anchor, so it stays on real data
  wherever the user scrolls the view.

Both windows are inclusive on both endpoints — the UI labels
("From" / "To") match. Same-day is a 1-day window, not 0.

URL state — the filter bar emits a `period` choice and nothing
more than the data needed to express it:

    ?period=last-30-days                    (a preset)
    ?period=custom&anchor=YYYY-MM-DD&view_days=N   (Custom)
    ?ref_days=N                             (Advanced reference)

Missing or invalid params → 30-day view ending today, with the
reference sample matching the view length (also 30 days), ending
on the most recent data.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta


DEFAULT_VIEW_DAYS = 30


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


@dataclass(frozen=True)
class WindowSelection:
    """The resolved filter state — the single model every view
    reads. `parse_windows` is the one place that produces it;
    nothing downstream re-decides dates. Views take the windows
    they need; the filter bar reads the display properties.
    """

    view: Window
    reference: Window
    # The resolved Period selection — always one of KNOWN_PERIODS.
    period: str

    @property
    def anchor(self) -> date:
        """The view window's end — also aging's 'as of' date."""
        return self.view.to

    @property
    def view_days(self) -> int:
        return self.view.days_inclusive

    @property
    def ref_days(self) -> int:
        return self.reference.days_inclusive

    @property
    def is_custom(self) -> bool:
        return self.period == "custom"

    @property
    def is_advanced(self) -> bool:
        """The Advanced panel is "in use" when the reference
        window has been decoupled from the view period — i.e. the
        statistical sample is a different length from the Period."""
        return self.ref_days != self.view_days


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


# The view-window presets, keyed by the `period` query value.
# Day-count presets map to a fixed inclusive length ending today;
# the two week presets are handled separately (last_completed_week).
VIEW_PRESET_DAYS: dict[str, int] = {
    "last-7-days": 7,
    "last-14-days": 14,
    "last-30-days": 30,
    "last-90-days": 90,
}
WEEK_PRESETS = ("last-week", "last-2-weeks")
DEFAULT_PERIOD = "last-30-days"
# `all-time` resolves the view to the full data span (data_min →
# data_max); useful for the CFD's "show everything" mode.
KNOWN_PERIODS = (*VIEW_PRESET_DAYS, *WEEK_PRESETS, "all-time", "custom")


def _parse_date(raw: object, default: date) -> date:
    """A YYYY-MM-DD string → date; anything malformed → `default`."""
    if raw:
        try:
            return date.fromisoformat(str(raw))
        except ValueError:
            pass
    return default


def _parse_days(raw: object, default: int) -> int:
    """A positive-int string → int; anything else → `default`."""
    if raw:
        try:
            n = int(raw)
            if n > 0:
                return n
        except ValueError:
            pass
    return default


def parse_windows(
    query: dict[str, str] | dict,
    today: date,
    data_max: date | None = None,
    data_min: date | None = None,
) -> WindowSelection:
    """The ONE place date math happens. The filter bar emits a
    `period` choice; this turns it into a `WindowSelection` —
    the single model every view and the filter bar read.

    **View window** — navigation, driven by `period`:
      - a preset (`last-7-days` … `last-2-weeks`) → a fixed
        window relative to `today`;
      - `custom` → `anchor` (default today) + `view_days`
        (default 30);
      - missing / unknown → the default preset (last 30 days).

    **Reference period** — the statistical sample for aging
    thresholds + forecasts. Its length follows the Period by
    default (`ref_days` overrides it); inclusive days ending on
    `data_max` (the most recent data), or `today` when the
    warehouse is empty. Anchored to the data, never to the view —
    so scrolling the view can't strand the percentile sample.

    `period` on the result is always resolved to a value in
    `KNOWN_PERIODS`. Never raises on user input.
    """
    raw = str(query.get("period") or "").strip()
    period = raw if raw in KNOWN_PERIODS else DEFAULT_PERIOD

    if period in VIEW_PRESET_DAYS:
        view = Window.last_n_days(VIEW_PRESET_DAYS[period], today=today)
    elif period == "last-week":
        view = last_completed_week(today)
    elif period == "last-2-weeks":
        end = last_completed_week(today).to
        view = Window(from_=end - timedelta(days=13), to=end)
    elif period == "all-time":
        # The whole data span. Falls back to the default preset
        # when the warehouse is empty (no data bounds to span).
        if data_min is not None and data_max is not None:
            view = Window(from_=data_min, to=data_max)
        else:
            view = Window.last_n_days(DEFAULT_VIEW_DAYS, today=today)
    else:  # "custom" — the only remaining KNOWN_PERIODS value
        anchor = _parse_date(query.get("anchor"), today)
        view = Window.last_n_days(
            _parse_days(query.get("view_days"), DEFAULT_VIEW_DAYS),
            today=anchor,
        )

    # Reference length follows the chosen Period by default: pick
    # "Last 7 days" and the percentile / forecast sample is 7 days
    # too. `?ref_days=` (Advanced) overrides it. The reference is
    # always anchored to `data_max` (most recent data), never the
    # view anchor — so scrolling the view can't strand the sample.
    ref_days = _parse_days(query.get("ref_days"), view.days_inclusive)
    reference = Window.last_n_days(ref_days, today=data_max or today)
    return WindowSelection(view=view, reference=reference, period=period)
