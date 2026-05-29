"""Layer 1 — warehouse connection bootstrap.

`open_warehouse(data_dir)` returns a DuckDB connection with the
`work_items` and `transitions` views registered against the
Parquet store. Both views deduplicate at read time so every
consumer sees one canonical row per identity — the latest
snapshot for work_items, exact-row distinct for transitions.

The SQL lives here, not in the FastAPI app layer.
"""

from __future__ import annotations

from pathlib import Path

import duckdb


def open_warehouse(data_dir: Path) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection with `work_items` and
    `transitions` views registered against the Parquet warehouse
    under `data_dir`.

    Each ETL run writes its OWN snapshot file —
    `year={Y}/month={M}/day={D}/items-{run_id}.parquet` — so every
    run (daily cron OR a browser-triggered backfill) is purely
    additive and never overwrites another. Runs accumulate, so the
    same item appears in N snapshots. The views deduplicate at read
    time so every consumer sees one canonical row per
    `(contract_id, source, item_id)` — the LATEST snapshot —
    without losing the on-disk history (useful for future
    "what did aging look like at snapshot X" features).

    `transitions` is similarly deduplicated. Transitions are
    append-only events — same `entered_at` for the same item
    should never collide — but a re-run that re-fetches the same
    item writes identical rows again, so DISTINCT collapses them.

    Both views fall back to empty stubs when the corresponding
    parquet files don't exist yet (fresh install before
    materialise has ever run).
    """
    con = duckdb.connect(":memory:")

    work_items_glob = (data_dir / "work_items" / "**" / "*.parquet").as_posix()
    # Latest snapshot per (contract_id, source, item_id) by
    # materialised_at. Stable tie-break by run_id keeps the
    # answer deterministic when two snapshots share the exact
    # same materialised_at (rare; same-second re-runs).
    # No parquet yet → propagate the IOException; work_items is
    # required by every component.
    con.execute(
        f"CREATE VIEW work_items AS "
        f"SELECT * EXCLUDE (_dedup_rn) FROM ( "
        f"  SELECT *, ROW_NUMBER() OVER ("
        f"    PARTITION BY contract_id, source, item_id "
        f"    ORDER BY materialised_at DESC, run_id DESC"
        f"  ) AS _dedup_rn "
        f"  FROM read_parquet('{work_items_glob}', hive_partitioning = true)"
        f") WHERE _dedup_rn = 1"
    )

    transitions_glob = (
        data_dir / "transitions" / "**" / "*.parquet"
    ).as_posix()
    try:
        # Keep every transition from the LATEST run per item. Remap
        # rewrites `stage` at materialise time, so re-running after a
        # step change yields the same (item_id, entered_at) with a
        # different stage — exact-row DISTINCT would keep both and
        # pollute the warehouse with stale vocab. DENSE_RANK by
        # (materialised_at, run_id) keeps all rows of the winning run
        # and drops superseded ones (NULLS LAST so any pre-upgrade rows
        # without the stamp lose to a re-materialised run).
        # union_by_name tolerates that schema bump across snapshots.
        con.execute(
            f"CREATE VIEW transitions AS "
            f"SELECT * EXCLUDE (_rr) FROM ( "
            f"  SELECT *, DENSE_RANK() OVER ( "
            f"    PARTITION BY contract_id, source, item_id "
            f"    ORDER BY materialised_at DESC NULLS LAST, "
            f"             run_id DESC NULLS LAST"
            f"  ) AS _rr "
            f"  FROM read_parquet('{transitions_glob}', "
            f"    hive_partitioning = true, union_by_name = true)"
            f") WHERE _rr = 1"
        )
    except duckdb.IOException:
        con.execute(
            "CREATE VIEW transitions AS "
            "SELECT NULL::VARCHAR AS source, "
            "NULL::VARCHAR AS item_id, "
            "NULL::TIMESTAMP AS entered_at, "
            "NULL::VARCHAR AS stage, "
            "NULL::VARCHAR AS signal, "
            "NULL::VARCHAR AS contract_id, "
            "NULL::TIMESTAMP AS materialised_at, "
            "NULL::VARCHAR AS run_id "
            "WHERE FALSE"
        )

    return con
