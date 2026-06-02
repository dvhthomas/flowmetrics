"""Empty `data_dir` (no `flow materialize` has ever run) must NOT
crash the warehouse open.

Repro path: user starts `flow serve` against a fresh install where
they've configured workflows via the wizard but haven't materialized
yet. Every chart fragment + every detail page tries to open the
warehouse → DuckDB throws IOException → 500. Worst part: the
Data Source page (where the user is supposed to GO to fix this)
also 500s.

Both views must fall back to empty stubs with the canonical schema.
Consumers then see "zero rows" instead of a crash, and the surviving
empty-state UI takes over.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
from starlette.testclient import TestClient

from flowmetrics.warehouse.connection import open_warehouse


class TestOpenWarehouseOnEmptyDataDir:
    def test_open_warehouse_does_not_raise_when_data_dir_is_empty(self, tmp_path):
        """Bug: today, open_warehouse raises duckdb.IOException with
        'No files found that match the pattern ...' the first time
        anything tries to read from the warehouse on a fresh install."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        con = open_warehouse(data_dir)
        # Sanity: the view exists and returns 0 rows.
        count = con.execute("SELECT COUNT(*) FROM work_items").fetchone()
        assert count == (0,)

    def test_transitions_view_already_empty_safe(self, tmp_path):
        """Regression: transitions already had the empty-stub path.
        Keep it green so the work_items fix doesn't bit-rot the
        transitions one."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        con = open_warehouse(data_dir)
        count = con.execute("SELECT COUNT(*) FROM transitions").fetchone()
        assert count == (0,)

    def test_work_items_stub_carries_canonical_columns(self, tmp_path):
        """When the stub fires, downstream queries that name specific
        columns must still parse — DuckDB validates column references
        at PREPARE time. The stub schema must mirror what materialize
        writes."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        con = open_warehouse(data_dir)
        # The canonical column set (matches materialize's writer +
        # what tests/test_slice1_e2e.py pins).
        required = {
            "source", "repo", "item_id", "title", "url", "author",
            "is_bot", "created_at", "completed_at", "cycle_time_days",
            "contract_id", "materialized_at", "run_id",
        }
        rel = con.execute("SELECT * FROM work_items LIMIT 0")
        cols = {d[0] for d in rel.description}
        missing = required - cols
        assert not missing, f"stub missing required columns: {missing}"


class TestServerOnEmptyDataDir:
    """End-to-end repro of the user's report: workflows.db has rows
    but data_dir is empty. The dashboard pages — especially Data
    Source, the place they're supposed to USE to fix this — must
    render rather than 500."""

    def _make_workflow_yaml(self, contracts_dir: Path, name: str) -> None:
        contracts_dir.mkdir(parents=True, exist_ok=True)
        (contracts_dir / f"{name}.yaml").write_text(
            "workflow:\n"
            f"  name: {name}\n"
            "  source: github\n"
            "  repo: owner/repo\n"
            "  start: 2026-04-01\n"
            "  stop: 2026-05-01\n"
        )

    def test_data_source_page_renders_when_warehouse_is_empty(self, tmp_path):
        from flowmetrics.app import create_app
        data_dir = tmp_path / "data"
        contracts_dir = tmp_path / "contracts"
        data_dir.mkdir()
        self._make_workflow_yaml(contracts_dir, "demo")

        app = create_app(data_dir=data_dir, contracts_dir=contracts_dir)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/workflows/demo/data-source")
        # The page the user goes to to RECOVER from this state must
        # render, even though no warehouse data exists yet.
        assert resp.status_code == 200, resp.text[:500]

    def test_workflow_detail_renders_when_warehouse_is_empty(self, tmp_path):
        from flowmetrics.app import create_app
        data_dir = tmp_path / "data"
        contracts_dir = tmp_path / "contracts"
        data_dir.mkdir()
        self._make_workflow_yaml(contracts_dir, "demo")

        app = create_app(data_dir=data_dir, contracts_dir=contracts_dir)
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/workflows/demo")
        assert resp.status_code == 200, resp.text[:500]
