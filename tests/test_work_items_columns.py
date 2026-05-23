"""Column-config refactor for the work-items table.

Until now, the table template hardcoded header structure (Title /
Started / Completed / Cycle) and the aging detail page worked
around the mismatch with `_in_flight_scope` branching in the
template. Adding a metric-specific column (Age) required template
edits and special-casing.

This refactor pushes the column list onto the data payload. The
route decides which columns to show; the template iterates without
branching. Sort affordances follow the column's declared
`sort_key`. The SQL-side sort whitelist remains, so column
re-ordering can't enable arbitrary `ORDER BY` injection.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.work_items_table import (
    Column,
    DEFAULT_COLUMNS,
    IN_FLIGHT_COLUMNS,
    render,
)

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    tmp = Path(tempfile.mkdtemp())
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"
    (contracts_dir / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
                    "name": "demo",
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
            "materialise", "demo",
            "--data-dir", str(data_dir),
            "--workflows-dir", str(contracts_dir),
            "--cache-dir", str(FIXTURE_CACHE),
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


class TestColumnConfig:
    def test_default_columns_cover_completed_view(self):
        """Default column set: id, title, started, completed,
        cycle. Pinned here so the dashboard / cycle-time-detail /
        throughput-detail tables stay consistent."""
        keys = [c.key for c in DEFAULT_COLUMNS]
        assert keys == [
            "item_id", "title", "created_at_display",
            "completed_at_display", "cycle_time_days",
        ]
        # All except item_id (the link) are sortable.
        sortable = {c.key for c in DEFAULT_COLUMNS if c.sort_key}
        assert "title" in sortable
        assert "completed_at_display" in sortable
        assert "cycle_time_days" in sortable

    def test_in_flight_columns_swap_completed_and_cycle_for_age(self):
        """The aging detail page's column set: drop Completed and
        Cycle (always None for in-flight), add Age."""
        keys = [c.key for c in IN_FLIGHT_COLUMNS]
        assert keys == [
            "item_id", "title", "created_at_display", "age_days",
        ]
        # Age has a quantitative kind so the template right-aligns it.
        age_col = next(c for c in IN_FLIGHT_COLUMNS if c.key == "age_days")
        assert age_col.align == "right"
        assert age_col.kind == "int"

    def test_render_default_returns_default_columns(self, warehouse):
        """Calling render() with no overrides puts DEFAULT_COLUMNS
        on the payload — the template doesn't have to know which
        view it's rendering for."""
        data = render(warehouse, "demo")
        assert data.columns == DEFAULT_COLUMNS

    def test_render_with_in_flight_at_uses_in_flight_columns(self, warehouse):
        """When the route opts into in-flight scope, the column
        set changes automatically. Routes don't have to remember
        to pass both `in_flight_at=X` and `columns=IN_FLIGHT_COLUMNS`
        — the component infers it."""
        data = render(warehouse, "demo", in_flight_at="2026-05-06")
        assert data.columns == IN_FLIGHT_COLUMNS

    def test_render_with_explicit_columns_overrides_defaults(self, warehouse):
        """A route can pass any column subset, e.g. for a compact
        per-author view or a future "stalled items" report."""
        custom = (
            Column(key="item_id", label="#", kind="id-link"),
            Column(key="title", label="Title", sort_key="title"),
        )
        data = render(warehouse, "demo", columns=custom)
        assert data.columns == custom

    def test_column_sort_key_is_validated_against_sql_whitelist(
        self, warehouse
    ):
        """Defense-in-depth: even if a Column carried a malicious
        `sort_key`, the render() call validates against a
        server-side whitelist and falls back to the default."""
        data = render(warehouse, "demo", sort="cycle_time_days; DROP TABLE x")
        # Falls back, no crash, no injection.
        assert data.sort == "completed_at"
