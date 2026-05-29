"""Overlapping backfills must not double-count in the charts.

Re-running materialise over an overlapping (or identical) window writes
additive snapshot files on disk — that's intentional. The read views
(`work_items`, `transitions`) must collapse those to exactly one
canonical copy per item (latest run), so every metric reads each item
once. This pins the dedup the dashboard depends on.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.warehouse.connection import open_warehouse

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"

_YAML = """contract:
  name: uv
  source: github
  repo: astral-sh/uv
  start: '2026-05-04'
  stop: '2026-05-10'
"""


def _materialise(contracts: Path, data: Path) -> None:
    res = CliRunner().invoke(
        cli,
        [
            "materialise", "uv",
            "--data-dir", str(data),
            "--workflows-dir", str(contracts),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output


def test_two_overlapping_backfills_dedupe_in_the_read_views(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    data = tmp_path / "data"
    (contracts / "uv.yaml").write_text(_YAML)

    _materialise(contracts, data)
    _materialise(contracts, data)  # same window again — overlap

    con = open_warehouse(data)

    # On disk, the same items now exist in two runs…
    raw = con.execute(
        "SELECT count(DISTINCT run_id) FROM read_parquet("
        f"'{(data / 'work_items' / '**' / '*.parquet').as_posix()}', "
        "hive_partitioning=true)"
    ).fetchone()[0]
    assert raw == 2, f"expected two runs on disk; got {raw}"

    # …but the work_items view shows each item exactly once.
    n, distinct = con.execute(
        "SELECT count(*), count(DISTINCT item_id) FROM work_items "
        "WHERE contract_id='uv'"
    ).fetchone()
    assert n == distinct and n > 0, f"work_items doubled: {n} rows / {distinct}"

    # Transitions come from a single (latest) run per item — no mixing.
    multi_run = con.execute(
        "SELECT count(*) FROM (SELECT item_id FROM transitions "
        "WHERE contract_id='uv' GROUP BY item_id "
        "HAVING count(DISTINCT run_id) > 1)"
    ).fetchone()[0]
    assert multi_run == 0, f"{multi_run} items have transitions from >1 run"

    # And no exact-duplicate transition rows survive.
    dup_rows = con.execute(
        "SELECT count(*) FROM (SELECT item_id, entered_at, stage, count(*) c "
        "FROM transitions WHERE contract_id='uv' GROUP BY 1,2,3 HAVING c > 1)"
    ).fetchone()[0]
    assert dup_rows == 0, f"{dup_rows} duplicate transition rows"
