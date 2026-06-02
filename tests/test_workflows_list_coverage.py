"""`flow workflows list --data-dir` surfaces whether each workflow
has warehouse data, so a CLI user spots the same empty-state the
dashboard now handles gracefully.

Without --data-dir, the output stays as it was (NAME / SOURCE /
TARGET). With --data-dir, a DATA column appears; the value is `ready`
if `flow materialize NAME` has ever produced Parquet under
`<data-dir>/work_items/contract_id=<name>/`, else `—`. When any
workflow is empty, a footer hint names the recovery command.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from click.testing import CliRunner

from flowmetrics.cli import cli


def _write_db_row(workflows_dir: Path, name: str, repo: str = "owner/repo") -> None:
    workflows_dir.mkdir(parents=True, exist_ok=True)
    db = workflows_dir / "workflows.db"
    yaml = (
        f"workflow:\n"
        f"  name: {name}\n"
        f"  source: github\n"
        f"  repo: {repo}\n"
        f"  start: 2026-04-01\n"
        f"  stop: 2026-05-01\n"
    )
    con = sqlite3.connect(db)
    try:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS workflows (
              id TEXT PRIMARY KEY, yaml TEXT NOT NULL,
              archived_at TEXT, archived_reason TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
        """)
        con.execute(
            "INSERT INTO workflows(id, yaml, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (name, yaml, "2026-05-01T00:00:00Z", "2026-05-01T00:00:00Z"),
        )
        con.commit()
    finally:
        con.close()


def _make_materialized_parquet(data_dir: Path, workflow_name: str) -> None:
    """Synthesise the minimal directory shape that signals 'materialize
    has run for this workflow at least once'."""
    leaf = (
        data_dir / "work_items"
        / f"contract_id={workflow_name}"
        / "year=2026" / "month=05" / "day=10"
    )
    leaf.mkdir(parents=True)
    (leaf / "items-run1.parquet").write_bytes(b"PAR1\x00\x00\x00FAKE")


class TestCoverageColumn:
    def test_no_data_dir_flag_keeps_legacy_output(self, tmp_path):
        wf = tmp_path / "contracts"
        _write_db_row(wf, "alpha")
        result = CliRunner().invoke(cli, [
            "workflows", "list", "--workflows-dir", str(wf),
        ], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        # Old output shape — no DATA column unless --data-dir given.
        assert "DATA" not in result.output

    def test_data_dir_with_materialized_workflow_shows_ready(self, tmp_path):
        wf = tmp_path / "contracts"
        data = tmp_path / "data"
        _write_db_row(wf, "alpha")
        _make_materialized_parquet(data, "alpha")

        result = CliRunner().invoke(cli, [
            "workflows", "list",
            "--workflows-dir", str(wf),
            "--data-dir", str(data),
        ], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "DATA" in result.output
        # Workflow has parquet → "ready".
        assert "ready" in result.output

    def test_data_dir_with_empty_warehouse_shows_dash_and_hint(self, tmp_path):
        wf = tmp_path / "contracts"
        data = tmp_path / "data"
        _write_db_row(wf, "alpha")
        data.mkdir()  # exists but empty

        result = CliRunner().invoke(cli, [
            "workflows", "list",
            "--workflows-dir", str(wf),
            "--data-dir", str(data),
        ], catch_exceptions=False)
        assert result.exit_code == 0, result.output
        assert "DATA" in result.output
        # The empty-cell marker.
        assert "—" in result.output or "-" in result.output
        # Recovery hint names a concrete command — must be runnable.
        out = result.output.lower()
        assert "flow materialize" in out or "data source" in out

    def test_mixed_workflows_show_per_row_status(self, tmp_path):
        wf = tmp_path / "contracts"
        data = tmp_path / "data"
        _write_db_row(wf, "ready-one")
        _write_db_row(wf, "empty-one")
        _make_materialized_parquet(data, "ready-one")
        # `empty-one` deliberately has no parquet.

        result = CliRunner().invoke(cli, [
            "workflows", "list",
            "--workflows-dir", str(wf),
            "--data-dir", str(data),
        ], catch_exceptions=False)
        assert result.exit_code == 0, result.output

        # Find each row in the output; check the DATA cell.
        lines = [ln for ln in result.output.splitlines() if ln.strip()]
        ready_line = next(ln for ln in lines if ln.startswith("ready-one"))
        empty_line = next(ln for ln in lines if ln.startswith("empty-one"))
        assert "ready" in ready_line
        assert "ready" not in empty_line
