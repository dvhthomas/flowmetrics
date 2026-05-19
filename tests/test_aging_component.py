"""Component tests for `flowmetrics.web.components.aging`.

Vacanti's Aging Work In Progress chart: in-flight items only,
plotted by current workflow state (x-axis) and elapsed age in
days (y-axis). Percentile lines drawn from completed-item cycle
times serve as checkpoints — once an item ages past the
commitment threshold (P85), it's likely to miss the forecast.

The component takes an `asof` UTC date (defaulting to today) so
historical views work: aging "as of last Friday" is a useful
forensic question, and the fixture's bounded window only has
in-flight items at intermediate as-of dates.
"""

from __future__ import annotations

import json
import re
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.aging import render

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
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
            "--contracts-dir",
            str(contracts_dir),
            "--cache-dir",
            str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output

    con = duckdb.connect(":memory:")
    for kind in ("work_items", "transitions"):
        glob = (data_dir / kind / "**" / "*.parquet").as_posix()
        con.execute(
            f"CREATE VIEW {kind} AS "
            f"SELECT * FROM read_parquet('{glob}', hive_partitioning = true)"
        )
    yield con
    con.close()


# A historical asof inside the fixture window — items completed on
# or after this date are "in flight" at this asof, with age =
# (asof - created_at.date()).days. May 06 is mid-week; items
# completed May 07–10 will appear as in-flight here.
_DEMO_ASOF = date(2026, 5, 6)


class TestAgingShape:
    def test_items_completed_after_asof_appear_as_in_flight(self, warehouse):
        """Aging includes items that started ≤ asof but didn't
        complete until after asof — those are the in-flight set
        from the asof's point of view."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        # Cross-check against the warehouse directly.
        in_flight_n = warehouse.execute(
            "SELECT count(*) FROM work_items "
            "WHERE contract_id = 'astral-uv-week' "
            "  AND created_at IS NOT NULL "
            "  AND CAST(created_at AS DATE) <= CAST(? AS DATE) "
            "  AND (completed_at IS NULL "
            "       OR CAST(completed_at AS DATE) > CAST(? AS DATE))",
            [_DEMO_ASOF, _DEMO_ASOF],
        ).fetchone()[0]
        assert in_flight_n > 0, "fixture sanity: should have ≥1 in-flight @ May 06"
        assert data.count == in_flight_n
        assert len(data.items) == in_flight_n

    def test_items_completed_by_asof_are_excluded(self, warehouse):
        """An item with completed_at ≤ asof is NOT in-flight at
        asof. Aging is about open work, not history."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        # Sample check: a known PR completed May 04 must not appear.
        completed_early = warehouse.execute(
            "SELECT item_id FROM work_items "
            "WHERE contract_id = 'astral-uv-week' "
            "  AND CAST(completed_at AS DATE) <= CAST(? AS DATE) "
            "LIMIT 1",
            [_DEMO_ASOF],
        ).fetchone()
        assert completed_early is not None
        ids = {i.item_id for i in data.items}
        assert completed_early[0] not in ids, (
            f"item {completed_early[0]!r} was completed by {_DEMO_ASOF} "
            f"and must not appear in aging"
        )

    def test_age_days_is_asof_minus_created(self, warehouse):
        """Age = (asof - created_at.date()) in calendar days."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        for item in data.items:
            created = warehouse.execute(
                "SELECT created_at FROM work_items "
                "WHERE contract_id = 'astral-uv-week' AND item_id = ?",
                [item.item_id],
            ).fetchone()[0]
            if created.tzinfo is None:
                created = created.replace(tzinfo=UTC)
            expected = (_DEMO_ASOF - created.date()).days
            assert item.age_days == expected, (
                f"item {item.item_id!r}: age_days={item.age_days} "
                f"expected={expected}"
            )

    def test_current_state_is_last_transition_at_or_before_asof(self, warehouse):
        """The state shown on the x-axis is the last stage the
        item ENTERED at or before asof. Transitions after asof
        haven't happened yet from the asof point of view."""
        asof = _DEMO_ASOF
        data = render(warehouse, "astral-uv-week", asof=asof)
        # Sample assertion against the transitions Parquet.
        if not data.items:
            return
        item = data.items[0]
        expected = warehouse.execute(
            "SELECT stage FROM transitions "
            "WHERE contract_id = 'astral-uv-week' AND item_id = ? "
            "  AND CAST(entered_at AS DATE) <= CAST(? AS DATE) "
            "ORDER BY entered_at DESC LIMIT 1",
            [item.item_id, asof],
        ).fetchone()
        assert expected is not None
        assert item.current_state == expected[0], (
            f"current_state for {item.item_id!r}: got "
            f"{item.current_state!r} expected {expected[0]!r}"
        )

    def test_default_asof_is_today_when_omitted(self, warehouse):
        """Default behavior: asof = today UTC. Against this fixture
        every item completed weeks ago, so default render is empty
        (0 in-flight items)."""
        data = render(warehouse, "astral-uv-week")
        assert data.count == 0
        assert data.asof_iso, "asof must be echoed back"
        # Today's UTC date in ISO form.
        from datetime import datetime
        today_iso = datetime.now(UTC).date().isoformat()
        assert data.asof_iso == today_iso

    def test_item_carries_identity_and_url(self, warehouse):
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        if not data.items:
            return
        first = data.items[0]
        assert first.item_id
        assert first.title
        assert first.url is None or first.url.startswith("http")

    def test_percentile_thresholds_come_from_completed_cycle_times(
        self, warehouse
    ):
        """Aging uses the SAME percentile thresholds the cycle-time
        chart shows — they're the commitment lines an aging item is
        checked against. Pull from completed cycle_time_days in the
        warehouse."""
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        row = warehouse.execute(
            "SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY cycle_time_days), "
            "       percentile_cont(0.85) WITHIN GROUP (ORDER BY cycle_time_days), "
            "       percentile_cont(0.95) WITHIN GROUP (ORDER BY cycle_time_days) "
            "FROM work_items "
            "WHERE contract_id = 'astral-uv-week' "
            "  AND cycle_time_days IS NOT NULL"
        ).fetchone()
        # Allow tiny float drift across reads.
        for got, want in zip([data.p50, data.p85, data.p95], row):
            assert abs(got - want) < 1e-9, (
                f"percentile drift: got {got!r} want {want!r}"
            )

    def test_headline_summarizes_item_count_and_asof(self, warehouse):
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        assert str(data.count) in data.headline, (
            f"headline must include the item count; got {data.headline!r}"
        )
        # Headline names the asof date in human form ("May 06, 2026").
        assert re.search(
            r"[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}", data.headline
        ), (
            f"headline must include a human-formatted asof date; "
            f"got {data.headline!r}"
        )

    def test_chart_spec_uses_point_marks_for_in_flight_items(self, warehouse):
        data = render(warehouse, "astral-uv-week", asof=_DEMO_ASOF)
        spec = json.loads(data.vega_spec_json())
        marks: list[str] = []

        def _collect(node):
            if isinstance(node, dict):
                m = node.get("mark")
                if isinstance(m, str):
                    marks.append(m)
                elif isinstance(m, dict) and "type" in m:
                    marks.append(m["type"])
                for v in node.values():
                    _collect(v)
            elif isinstance(node, list):
                for v in node:
                    _collect(v)

        _collect(spec)
        # Point + rule layers (rule for percentile thresholds).
        assert "point" in marks, (
            f"aging spec must include point marks; got {marks}"
        )
        assert "rule" in marks, (
            f"aging spec must include rule marks for percentile "
            f"thresholds; got {marks}"
        )
