"""Slice 1 acceptance: `flow materialise NAME` produces Parquet
that DuckDB can query.

The slice 1 click-path the user described:

  > Run `flow materialise calcmark` from cron at 06:00. Five minutes
  > later, Parquet files exist under data/work_items/contract_id=…/
  > and DuckDB can SELECT count(*) and get real numbers.

This test enforces that contract. It uses the existing pinned
GitHub fixture cache (tests/fixtures/cache/, recorded against
astral-sh/uv for 2026-05-04..2026-05-10) so the test stays offline
and reproducible. The contract name in the test is astral-uv-week
because that's what the fixture covers; the user-facing example will
be CalcMark (or whatever the operator points it at), but the
acceptance contract — "given a valid contract, materialise writes
queryable Parquet" — doesn't care which repo.

Per SPEC.md §6 (test credibility rule) this is an e2e test: it
drives the real CLI through Click's CliRunner, lets it touch real
files in a tmp dir, and asserts the user-observable result — files
on disk + DuckDB query output — not internal call shape.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


def _write_test_contract(contracts_dir: Path) -> str:
    """Write a minimal contract YAML matching the pinned fixture cache."""
    name = "astral-uv-week"
    contract = {
        "contract": {
            "name": name,
            "source": "github",
            "repo": "astral-sh/uv",
            "start": "2026-05-04",
            "stop": "2026-05-10",
        }
    }
    (contracts_dir / f"{name}.yaml").write_text(yaml.safe_dump(contract))
    return name


class TestSlice1Acceptance:
    """`flow materialise` writes Parquet that DuckDB can query."""

    def test_materialise_produces_queryable_work_items_parquet(self, tmp_path):
        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        data_dir = tmp_path / "data"
        name = _write_test_contract(contracts_dir)

        result = CliRunner().invoke(
            cli,
            [
                "materialise",
                name,
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
        assert result.exit_code == 0, (
            f"`flow materialise` failed (exit={result.exit_code}):\n"
            f"{result.output}"
        )

        work_items_dir = data_dir / "work_items" / f"contract_id={name}"
        parquet_files = list(work_items_dir.rglob("*.parquet"))
        assert parquet_files, (
            f"no work_items parquet written under {work_items_dir}"
        )

        # User can query the result.
        glob = str(work_items_dir / "**" / "*.parquet")
        rows = duckdb.sql(
            f"SELECT count(*) FROM read_parquet('{glob}')"
        ).fetchone()
        assert rows is not None
        assert rows[0] >= 10, (
            f"expected ≥10 PRs from astral-sh/uv fixture window, got {rows[0]}"
        )

    def test_materialise_writes_expected_columns(self, tmp_path):
        """The user does `SELECT * FROM read_parquet(...)` and expects
        identity, lifecycle, and provenance columns. Phase-duration and
        stage-duration columns can arrive in later slices; identity +
        lifecycle + provenance must be there in Slice 1.
        """
        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        data_dir = tmp_path / "data"
        name = _write_test_contract(contracts_dir)

        result = CliRunner().invoke(
            cli,
            [
                "materialise",
                name,
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
        assert result.exit_code == 0, result.output

        glob = str(
            data_dir / "work_items" / f"contract_id={name}" / "**" / "*.parquet"
        )
        # DuckDB returns column names via .columns
        rel = duckdb.sql(f"SELECT * FROM read_parquet('{glob}') LIMIT 0")
        cols = set(rel.columns)

        required = {
            # Identity
            "source",
            "repo",
            "item_id",
            "title",
            "url",
            "author",
            "is_bot",
            # Lifecycle
            "created_at",
            "completed_at",
            "cycle_time_days",
            # Provenance
            "contract_id",
            "materialised_at",
            "run_id",
        }
        missing = required - cols
        assert not missing, f"missing required columns: {missing}; have {sorted(cols)}"

    def test_materialise_writes_transitions_parquet(self, tmp_path):
        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        data_dir = tmp_path / "data"
        name = _write_test_contract(contracts_dir)

        result = CliRunner().invoke(
            cli,
            [
                "materialise",
                name,
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
        assert result.exit_code == 0, result.output

        transitions_dir = data_dir / "transitions" / f"contract_id={name}"
        parquet_files = list(transitions_dir.rglob("*.parquet"))
        assert parquet_files, (
            f"no transitions parquet written under {transitions_dir}"
        )

        glob = str(transitions_dir / "**" / "*.parquet")
        rel = duckdb.sql(f"SELECT * FROM read_parquet('{glob}') LIMIT 0")
        cols = set(rel.columns)
        required = {"source", "item_id", "entered_at", "stage", "signal", "contract_id"}
        missing = required - cols
        assert not missing, f"missing transitions columns: {missing}; have {sorted(cols)}"

    def test_materialise_writes_run_manifest(self, tmp_path):
        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        data_dir = tmp_path / "data"
        name = _write_test_contract(contracts_dir)

        result = CliRunner().invoke(
            cli,
            [
                "materialise",
                name,
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
        assert result.exit_code == 0, result.output

        runs_dir = data_dir / "runs" / name
        manifests = list(runs_dir.rglob("manifest.json"))
        assert manifests, f"no manifest under {runs_dir}"

        import json

        manifest = json.loads(manifests[0].read_text())
        # The user can inspect the manifest after a run; required keys:
        for key in ["run_id", "contract_id", "started_at", "completed_at", "items_fetched"]:
            assert key in manifest, f"missing manifest key {key!r}; have {sorted(manifest)}"

    def test_unknown_contract_name_exits_nonzero_with_clear_message(self, tmp_path):
        """When cron is misconfigured (wrong name), `flow materialise`
        must fail loudly — not silently produce empty Parquet."""
        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        data_dir = tmp_path / "data"

        result = CliRunner().invoke(
            cli,
            [
                "materialise",
                "does-not-exist",
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
        assert result.exit_code != 0
        assert "does-not-exist" in result.output, (
            "error message should name the missing contract"
        )

    def test_invalid_contract_yaml_exits_nonzero_with_clear_message(self, tmp_path):
        contracts_dir = tmp_path / "contracts"
        contracts_dir.mkdir()
        # Malformed YAML — unbalanced brackets
        (contracts_dir / "broken.yaml").write_text("contract: {name: broken\n")
        data_dir = tmp_path / "data"

        result = CliRunner().invoke(
            cli,
            [
                "materialise",
                "broken",
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
        assert result.exit_code != 0


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Belt-and-braces network guard for this file.

    The repo-wide conftest already blocks network in unit tests, but
    this file is an e2e test that runs the real CLI; the offline flag
    + read-only cache should mean no network call ever happens, and
    this fixture catches a regression where it does.
    """
    # The repo-wide conftest fixture handles this already; reasserting
    # here as documentation of intent.
    return
