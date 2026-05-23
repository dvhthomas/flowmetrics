"""Dashboard scope-by-section structural contract.

The dashboard groups tiles by what scope drives them:

  - Snapshot section (Aging WIP) — pinned to the latest
    materialise; no Period picker applies.
  - Windowed section (Throughput, Cycle Time, Cumulative Flow,
    Forecast) — the Period + Reference picker that drives them
    lives in this section's header.

The page-top filter bar is gone — the picker lives WITH the
section it controls.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner
from fastapi.testclient import TestClient

from flowmetrics.app import create_app
from flowmetrics.cli import cli

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("dash-sections")
    contracts = tmp / "contracts"
    contracts.mkdir()
    data = tmp / "data"
    (contracts / "astral-uv-week.yaml").write_text(
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
        cli, [
            "materialise", "astral-uv-week",
            "--data-dir", str(data),
            "--workflows-dir", str(contracts),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output
    return create_app(data_dir=data, contracts_dir=contracts)


@pytest.fixture(scope="module")
def html(app) -> str:
    with TestClient(app) as client:
        r = client.get("/workflows/astral-uv-week")
    assert r.status_code == 200
    return r.text


class TestPageTopFilterBarIsGone:
    def test_dashboard_has_no_top_level_filter_nav(self, html):
        # The page-top `<nav class="filter-bar">` is the lie this
        # restructure removes — its controls don't drive Aging.
        assert 'class="filter-bar"' not in html


class TestSnapshotSection:
    def test_snapshot_section_has_a_current_state_heading(self, html):
        assert "scope-section--snapshot" in html
        body = html.split('scope-section--snapshot', 1)[1]
        next_section = body.find('scope-section--')
        snapshot_block = body[:next_section] if next_section >= 0 else body
        assert "Current state" in snapshot_block

    def test_aging_tile_is_inside_the_snapshot_section(self, html):
        body = html.split('scope-section--snapshot', 1)[1]
        next_section = body.find('scope-section--')
        snapshot_block = body[:next_section] if next_section >= 0 else body
        assert "dashboard-tile/aging" in snapshot_block


class TestWindowedSection:
    def test_windowed_section_has_a_time_slice_heading_with_a_period_picker(self, html):
        assert "scope-section--window" in html
        body = html.split('scope-section--window', 1)[1]
        assert "Time slice" in body[:5000]
        # Period select lives INSIDE this section now (not page-top).
        assert 'name="period"' in body[:5000]

    def test_windowed_section_holds_the_four_windowed_tiles(self, html):
        body = html.split('scope-section--window', 1)[1]
        for slug in (
            "dashboard-tile/throughput",
            "dashboard-tile/cycle-time",
            "dashboard-tile/cfd",
            "dashboard-tile/forecast",
        ):
            assert slug in body, f"windowed section must include {slug}"


class TestOrdering:
    def test_aging_renders_before_the_windowed_tiles(self, html):
        # Vertical order: snapshot section above windowed section.
        snap = html.find("scope-section--snapshot")
        win = html.find("scope-section--window")
        assert snap >= 0 and win >= 0
        assert snap < win, "snapshot (Aging) must appear before the windowed group"


class TestDataSourceStrip:
    """One compact line under the header naming the data source +
    the latest materialise date, linking to /data-source. Replaces
    both the old 'Data source & backfill →' link and the stale-data
    banner inside the filter form."""

    def test_strip_names_source_and_latest_materialise_date(self, html):
        assert "data-source-strip" in html
        # Source-display name (GitHub / Jira) and 'data through'.
        assert ("GitHub data through" in html) or ("Jira data through" in html)

    def test_strip_links_to_the_data_source_page(self, html):
        # The clickable strip points at the workflow's data-source page.
        assert (
            '/workflows/astral-uv-week/data-source'
        ) in html

    def test_stale_banner_is_gone_from_the_filter_form(self, html):
        # The old `filter-stale-banner` is retired; freshness lives
        # in the data-source strip now.
        assert "filter-stale-banner" not in html


class TestSnapshotIsPreservedAcrossPeriodChange:
    """Changing the Period dropdown shouldn't re-render the Aging
    chart — Aging is in the snapshot scope, decoupled from Period.

    We pair `hx-boost` on the window-filter form (so submit is an
    XHR body swap, not a full reload) with `hx-preserve` on the
    snapshot section (so it sits out the swap)."""

    def test_period_form_is_hx_boosted(self, html):
        body = html.split('scope-section--window', 1)[1]
        # The form inside the windowed-section header carries
        # hx-boost so its submit becomes a swap, not a reload.
        assert 'hx-boost="true"' in body[:5000], (
            "windowed-section form must be hx-boosted so submitting "
            "the Period dropdown does a body swap (not a full reload)"
        )

    def test_snapshot_section_is_hx_preserved(self, html):
        snap_html = html.split(
            'scope-section--snapshot', 1
        )[1].split('scope-section--window', 1)[0]
        assert 'hx-preserve="true"' in snap_html
        # hx-preserve requires a stable id.
        assert 'id="snapshot-section"' in snap_html or 'id="aging-section"' in snap_html
