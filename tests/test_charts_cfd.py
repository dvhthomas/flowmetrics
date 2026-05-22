"""Layer 2 (chart model) — tests for `flowmetrics.charts.cfd`.

`build_cfd_model` is pure: raw `StageEntry` rows + a stage order +
a view window in, a fully-resolved `CfdModel` out. The Vacanti
invariants (cumulative-only, earlier-dominates-later) and the
visual window clamping are asserted here, with no DuckDB and no
Vega. `infer_stage_order` is tested as a separate pure function.
"""

from __future__ import annotations

from datetime import date

from flowmetrics.charts.cfd import (
    build_cfd_model,
    infer_stage_order,
)
from flowmetrics.warehouse.queries import StageEntry
from flowmetrics.windows import Window


def _entry(item_id: str, stage: str, d: date) -> StageEntry:
    return StageEntry(item_id=item_id, stage=stage, entered_date=d)


class TestInferStageOrder:
    def test_pure_precedence(self):
        # A precedes B (5×), B precedes C (3×).
        order = infer_stage_order(
            [("A", "B", 5), ("B", "C", 3)], ["A", "B", "C"],
        )
        assert order == ("A", "B", "C")

    def test_alphabetical_tiebreak_when_no_pairs(self):
        assert infer_stage_order([], ["B", "A"]) == ("A", "B")

    def test_empty_stages(self):
        assert infer_stage_order([], []) == ()

    def test_propagates_through_pair_chain(self):
        order = infer_stage_order(
            [("A", "B", 1), ("B", "C", 1), ("A", "C", 1)],
            ["A", "B", "C"],
        )
        assert order == ("A", "B", "C")


class TestBuildCfdModel:
    def test_empty_for_no_entries(self):
        assert build_cfd_model([], ("A",)).is_empty

    def test_empty_for_no_stages(self):
        assert build_cfd_model([_entry("#1", "A", date(2026, 1, 1))], ()).is_empty

    def test_daily_spans_the_data_range_inclusive(self):
        # data range: Jan 1..5 = 5 days
        entries = [
            _entry("#1", "A", date(2026, 1, 1)),
            _entry("#1", "B", date(2026, 1, 3)),
            _entry("#2", "A", date(2026, 1, 2)),
            _entry("#2", "B", date(2026, 1, 5)),
        ]
        m = build_cfd_model(entries, ("A", "B"))
        assert len(m.daily) == 5
        assert m.first_date_iso == "2026-01-01"
        assert m.last_date_iso == "2026-01-05"

    def test_cumulative_only_increases(self):
        """Vacanti property #2 — counts never decrease."""
        entries = [
            _entry("#1", "A", date(2026, 1, 1)),
            _entry("#1", "B", date(2026, 1, 3)),
            _entry("#2", "A", date(2026, 1, 2)),
            _entry("#2", "B", date(2026, 1, 5)),
        ]
        m = build_cfd_model(entries, ("A", "B"))
        for stage in ("A", "B"):
            seq = [d.counts[stage] for d in m.daily]
            assert seq == sorted(seq)

    def test_earlier_stage_dominates_later(self):
        """Vacanti property #3 — count(earlier) >= count(later)
        for every date and every adjacent pair."""
        entries = [
            _entry("#1", "A", date(2026, 1, 1)),
            _entry("#1", "B", date(2026, 1, 3)),
            _entry("#2", "A", date(2026, 1, 2)),
        ]
        m = build_cfd_model(entries, ("A", "B"))
        for d in m.daily:
            assert d.counts["A"] >= d.counts["B"]

    def test_terminal_cumulative_counts_arrivals_at_terminal(self):
        """Property #1 — terminal cumulative at the final date
        equals the count of items that ever entered the terminal."""
        entries = [
            _entry("#1", "A", date(2026, 1, 1)),
            _entry("#1", "B", date(2026, 1, 3)),
            _entry("#2", "A", date(2026, 1, 2)),
            _entry("#2", "B", date(2026, 1, 5)),
        ]
        m = build_cfd_model(entries, ("A", "B"))
        assert m.daily[-1].counts["B"] == 2

    def test_items_skipping_a_stage_propagate_backwards(self):
        """An item that enters a later stage without ever recording
        an earlier-stage entry must still be counted in the earlier
        stage from that date forward — otherwise the bands cross."""
        # #1 only ever has a B-entry; the A-band must still count it.
        entries = [_entry("#1", "B", date(2026, 1, 1))]
        m = build_cfd_model(entries, ("A", "B"))
        assert m.daily[-1].counts["A"] >= m.daily[-1].counts["B"]


class TestVisualWindow:
    def test_view_clamps_to_data_range(self):
        entries = [_entry("#1", "A", date(2026, 1, 5))]
        m = build_cfd_model(
            entries, ("A",),
            view=Window(from_=date(2020, 1, 1), to=date(2030, 1, 1)),
        )
        assert m.first_date_iso == "2026-01-05"
        assert m.last_date_iso == "2026-01-05"

    def test_window_inside_data(self):
        entries = [
            _entry(f"#{i}", "A", date(2026, 1, i)) for i in range(1, 11)
        ]
        m = build_cfd_model(
            entries, ("A",),
            view=Window(from_=date(2026, 1, 3), to=date(2026, 1, 5)),
        )
        assert m.first_date_iso == "2026-01-03"
        assert m.last_date_iso == "2026-01-05"
        assert len(m.daily) == 3

    def test_window_entirely_outside_data_is_empty(self):
        entries = [_entry("#1", "A", date(2026, 1, 1))]
        m = build_cfd_model(
            entries, ("A",),
            view=Window(from_=date(2027, 1, 1), to=date(2028, 1, 1)),
        )
        assert m.is_empty


class TestCrop:
    def _bulk(self, *, stages=("A", "B")) -> list[StageEntry]:
        """Five items that enter A then B on the same day i=1..5 —
        gives a non-trivial terminal carry-in for a mid-window."""
        out: list[StageEntry] = []
        for i in range(1, 6):
            out.append(_entry(f"#{i}", "A", date(2026, 1, i)))
            out.append(_entry(f"#{i}", "B", date(2026, 1, i)))
        return out

    def test_no_crop_at_dawn_of_data(self):
        """A full-history window starts when the first arrival
        happens — no carry-in to crop."""
        m = build_cfd_model(self._bulk(), ("A", "B"))
        assert m.crop is None

    def test_crop_max_is_the_left_edge_carry_in(self):
        """A view that starts partway in has a carry-in at its left
        edge — the crop slider runs from 0 up to that count."""
        m = build_cfd_model(
            self._bulk(), ("A", "B"),
            view=Window(from_=date(2026, 1, 3), to=date(2026, 1, 5)),
        )
        assert m.crop is not None
        # items with a B-entry by Jan 3 = #1, #2, #3 → 3.
        assert m.crop.ceiling == 3
        assert m.crop.floor == 0
        assert m.crop.default == 0  # opens at 0 = no crop


class TestHeadline:
    def test_headline_names_counts_and_window(self):
        entries = [
            _entry("#1", "A", date(2026, 1, 1)),
            _entry("#1", "B", date(2026, 1, 3)),
            _entry("#2", "A", date(2026, 1, 2)),
            _entry("#2", "B", date(2026, 1, 5)),
        ]
        m = build_cfd_model(entries, ("A", "B"))
        h = m.headline
        assert "2 items touched" in h
        assert "2 departed" in h
        assert "0 in the system" in h
