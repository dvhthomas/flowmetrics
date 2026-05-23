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
        # Transitions are append-only stage-entry events; identical
        # rows across snapshots collapse via DISTINCT. No
        # materialised_at column to order by (transitions don't
        # carry one), but exact-row dedup is enough.
        con.execute(
            f"CREATE VIEW transitions AS "
            f"SELECT DISTINCT * FROM read_parquet("
            f"'{transitions_glob}', hive_partitioning = true)"
        )
    except duckdb.IOException:
        con.execute(
            "CREATE VIEW transitions AS "
            "SELECT NULL::VARCHAR AS source, "
            "NULL::VARCHAR AS item_id, "
            "NULL::TIMESTAMP AS entered_at, "
            "NULL::VARCHAR AS stage, "
            "NULL::VARCHAR AS signal, "
            "NULL::VARCHAR AS contract_id "
            "WHERE FALSE"
        )

    return con
