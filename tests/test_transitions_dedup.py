"""The `transitions` view must keep only the LATEST run's rows per item.

Remap-at-materialise rewrites a transition's `stage`, so re-running after
a step change yields the same (item_id, entered_at) with a different
stage. The old `SELECT DISTINCT *` view kept both, polluting the
warehouse with stale stage vocab. The view now dedups by latest run
(materialised_at, run_id) like `work_items` does.
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb

from flowmetrics.canonical import StageTransition
from flowmetrics.materialise import _write_transitions_parquet
from flowmetrics.warehouse.connection import open_warehouse


def _tx(stage: str) -> StageTransition:
    return StageTransition(
        item_id="i1",
        entered_at=datetime(2026, 5, 1, tzinfo=UTC),
        stage=stage,
        signal="github-pr-created",
    )


def _minimal_work_items(data_dir, contract_id="c"):
    """open_warehouse requires a work_items parquet to exist."""
    p = (
        data_dir / "work_items" / f"contract_id={contract_id}"
        / "year=2026" / "month=05" / "day=02"
    )
    p.mkdir(parents=True)
    con = duckdb.connect()
    con.execute(
        "CREATE TABLE wi (source VARCHAR, item_id VARCHAR, contract_id VARCHAR, "
        "materialised_at TIMESTAMP, run_id VARCHAR)"
    )
    con.execute("INSERT INTO wi VALUES ('github','i1','c',TIMESTAMP '2026-05-02 11:00:00','NEW')")
    con.execute(f"COPY wi TO '{(p / 'items-NEW.parquet').as_posix()}' (FORMAT PARQUET)")
    con.close()


def test_transitions_view_keeps_only_the_latest_run(tmp_path):
    base = (
        tmp_path / "transitions" / "contract_id=c"
        / "year=2026" / "month=05" / "day=02"
    )
    base.mkdir(parents=True)
    # Old run: raw stage.
    _write_transitions_parquet(
        transitions=[_tx("Awaiting Review")],
        contract_source="github", contract_id="c",
        out_path=base / "transitions-OLD.parquet",
        materialised_at=datetime(2026, 5, 2, 10, tzinfo=UTC), run_id="OLD",
    )
    # New run: same item + entered_at, remapped stage.
    _write_transitions_parquet(
        transitions=[_tx("In Review")],
        contract_source="github", contract_id="c",
        out_path=base / "transitions-NEW.parquet",
        materialised_at=datetime(2026, 5, 2, 11, tzinfo=UTC), run_id="NEW",
    )
    _minimal_work_items(tmp_path)

    con = open_warehouse(tmp_path)
    stages = [
        r[0]
        for r in con.execute(
            "SELECT stage FROM transitions WHERE item_id = 'i1'"
        ).fetchall()
    ]
    assert stages == ["In Review"], (
        f"only the latest run's stage should survive; got {stages}"
    )
