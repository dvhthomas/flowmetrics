"""Anchor + reference behaviour for `WorkflowView`.

`WorkflowView` holds the one `WindowSelection` model (produced
by `parse_windows`); it does not re-decide dates. The model:

  - The **view window** is driven by the `period` selection. A
    preset is relative to today; `period=custom` honours an
    explicit `anchor` + `view_days` verbatim — never clamped to
    the data. A stale workflow loads on today's (empty) window
    and the NODATA state explains it.
  - The **reference period** is anchored to the most recent data
    (`data_max`), independent of the view.
  - Aging's `asof` is pinned to the latest materialise (the
    in-flight snapshot date), independent of the Period picker.
  - The completion-data coverage (`data_min_date` /
    `data_max_date`) is exposed so the filter-bar date input can
    be bounded to dates that actually have data.
"""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.app import WorkflowView
from flowmetrics.cli import cli

from tests._window_helpers import window_query

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def view_factory():
    """Materialise the `astral-uv-week` fixture (data May 4–10,
    2026) and return a builder for `WorkflowView` instances."""
    tmp = Path(tempfile.mkdtemp())
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"
    (contracts_dir / "astral-uv-week.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
                    "name": "astral-uv-week",
                    "source": "github",
                    "repo": "astral-sh/uv",
                    "start": "2026-05-04",
                    "stop": "2026-05-10",
                }
            }
        )
    )
    res = CliRunner().invoke(
        cli,
        [
            "materialise",
            "astral-uv-week",
            "--data-dir",
            str(data_dir),
            "--workflows-dir",
            str(contracts_dir),
            "--cache-dir",
            str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output

    def _make(query: dict | None = None) -> WorkflowView:
        return WorkflowView(
            "astral-uv-week",
            contracts_dir=contracts_dir,
            data_dir=data_dir,
            query=query,
        )

    return _make


class TestViewAnchor:
    def test_default_period_view_ends_today(self, view_factory):
        """No query params → the default preset (last 30 days),
        whose view window ends today UTC. A stale warehouse must
        not drag the view backward."""
        sel = view_factory().selection
        assert sel.view.to == datetime.now(UTC).date()
        assert sel.period == "last-30-days"

    def test_custom_anchor_is_honoured_verbatim_even_past_data(
        self, view_factory
    ):
        """`period=custom` uses the anchor as-is — no clamp. An
        empty result is the NODATA state's job, not a silent jump."""
        sel = view_factory(
            window_query(custom_ending="2027-01-01", view_days=30)
        ).selection
        assert sel.view.to == date(2027, 1, 1)
        assert sel.view.days_inclusive == 30
        assert sel.period == "custom"

    def test_unknown_period_falls_back_to_default(self, view_factory):
        sel = view_factory(window_query(preset="bananas")).selection
        assert sel.period == "last-30-days"

    def test_no_clamp_attributes_remain(self, view_factory):
        """The old clamp bookkeeping is gone — nothing reports a
        'requested vs clamped' anchor anymore."""
        view = view_factory(window_query(custom_ending="2027-01-01"))
        assert not hasattr(view, "anchor_clamped")
        assert not hasattr(view, "requested_anchor")


class TestReferenceAnchor:
    def test_reference_anchors_to_data_max_not_the_view(self, view_factory):
        """The reference period ends on the most recent data,
        regardless of where the view anchor sits. Its length
        follows the view period."""
        view = view_factory(
            window_query(custom_ending="2027-01-01", view_days=14)
        )
        assert view.selection.reference.to == view.data_max_date
        assert view.selection.ref_days == view.selection.view_days == 14

    def test_reference_duration_is_tweakable_via_ref_days(
        self, view_factory
    ):
        """`ref_days` (the Advanced control) sets the reference
        length; it still ends on the data, not the view anchor."""
        view = view_factory(window_query(ref_days=60))
        assert view.selection.reference.to == view.data_max_date
        assert view.selection.ref_days == 60
        assert view.selection.is_advanced is True

    def test_default_reference_is_not_advanced(self, view_factory):
        """A default-length reference keeps the Advanced panel
        closed."""
        assert view_factory().selection.is_advanced is False


class TestDataCoverage:
    def test_exposes_the_completion_data_range(self, view_factory):
        """`data_min_date` / `data_max_date` bound the filter-bar
        date input. For this fixture they sit inside May 2026."""
        view = view_factory()
        assert view.data_min_date is not None
        assert view.data_max_date is not None
        assert view.data_min_date <= view.data_max_date
        assert date(2026, 5, 4) <= view.data_min_date <= date(2026, 5, 10)
        assert date(2026, 5, 4) <= view.data_max_date <= date(2026, 5, 10)


class TestAgingPinnedToSnapshot:
    def test_render_aging_asof_is_the_snapshot_not_the_anchor(
        self, view_factory
    ):
        """Aging is a "right now" snapshot — its `asof` is pinned to
        the latest materialise (the in-flight snapshot date), NOT
        the Period anchor. A custom anchor in the past must not
        move it."""
        view = view_factory(window_query(custom_ending="2026-05-06"))
        with view.warehouse() as con:
            snapshot = con.execute(
                "SELECT max(materialised_at) FROM work_items "
                "WHERE contract_id = ?",
                ["astral-uv-week"],
            ).fetchone()[0]
            aging = view.render_aging(con)
        assert aging.asof_iso == snapshot.date().isoformat()
        # The Period anchor (2026-05-06) must NOT drive aging.
        assert aging.asof_iso != "2026-05-06"


# NOTE: the throughput-coverage assertion that lived here checked
# that the view's `render_throughput` wrapper forwarded
# warehouse_start/warehouse_stop. The model now derives coverage
# from its own input items (see `test_charts_throughput`), so the
# wrapper no longer threads those args.
