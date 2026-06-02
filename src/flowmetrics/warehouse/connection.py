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
    materialize has ever run).
    """
    con = duckdb.connect(":memory:")

    work_items_glob = (data_dir / "work_items" / "**" / "*.parquet").as_posix()
    # Latest snapshot per (contract_id, source, item_id) by
    # materialized_at. Stable tie-break by run_id keeps the
    # answer deterministic when two snapshots share the exact
    # same materialized_at (rare; same-second re-runs).
    try:
        con.execute(
            f"CREATE VIEW work_items AS "
            f"SELECT * EXCLUDE (_dedup_rn) FROM ( "
            f"  SELECT *, ROW_NUMBER() OVER ("
            f"    PARTITION BY contract_id, source, item_id "
            f"    ORDER BY materialized_at DESC, run_id DESC"
            f"  ) AS _dedup_rn "
            f"  FROM read_parquet('{work_items_glob}', hive_partitioning = true)"
            f") WHERE _dedup_rn = 1"
        )
    except duckdb.IOException:
        # No parquet yet (fresh install before `flow materialize` has
        # ever run). Stub the view with the canonical schema so
        # downstream queries that name specific columns still PREPARE
        # cleanly. Empty result set → consumers' empty-state UI
        # ("no data yet, backfill from the Data Source page") takes
        # over. Without this stub, the Data Source page itself
        # 500s — the very page the user is supposed to go to.
        con.execute(
            "CREATE VIEW work_items AS "
            "SELECT NULL::VARCHAR  AS source, "
            "NULL::VARCHAR  AS repo, "
            "NULL::VARCHAR  AS item_id, "
            "NULL::VARCHAR  AS title, "
            "NULL::VARCHAR  AS url, "
            "NULL::VARCHAR  AS author, "
            "NULL::BOOLEAN  AS is_bot, "
            "NULL::TIMESTAMP AS created_at, "
            "NULL::TIMESTAMP AS completed_at, "
            "NULL::DOUBLE   AS cycle_time_days, "
            "NULL::VARCHAR  AS contract_id, "
            "NULL::TIMESTAMP AS materialized_at, "
            "NULL::VARCHAR  AS run_id "
            "WHERE FALSE"
        )

    transitions_glob = (
        data_dir / "transitions" / "**" / "*.parquet"
    ).as_posix()
    try:
        # Keep every transition from the LATEST run per item. Remap
        # rewrites `stage` at materialize time, so re-running after a
        # step change yields the same (item_id, entered_at) with a
        # different stage — exact-row DISTINCT would keep both and
        # pollute the warehouse with stale vocab. DENSE_RANK by
        # (materialized_at, run_id) keeps all rows of the winning run
        # and drops superseded ones (NULLS LAST so any pre-upgrade rows
        # without the stamp lose to a re-materialized run).
        # union_by_name tolerates that schema bump across snapshots.
        con.execute(
            f"CREATE VIEW transitions AS "
            f"SELECT * EXCLUDE (_rr) FROM ( "
            f"  SELECT *, DENSE_RANK() OVER ( "
            f"    PARTITION BY contract_id, source, item_id "
            f"    ORDER BY materialized_at DESC NULLS LAST, "
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
            "NULL::TIMESTAMP AS materialized_at, "
            "NULL::VARCHAR AS run_id "
            "WHERE FALSE"
        )

    return con
