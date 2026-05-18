"""ETL: contract → source fetch → canonical → Parquet.

Invoked by `flow materialise NAME`. One-shot:

  1. Load contract YAML.
  2. Build source adapter (GitHub or Jira) using existing factories.
  3. Fetch completed items in the contract's window.
  4. Convert each WorkItem → fact-table row (work_items.parquet).
  5. Convert each WorkItem.status_intervals → StageTransition rows
     (transitions.parquet).
  6. Write Parquet atomically (write `.tmp`, rename).
  7. Append a run manifest under `data/runs/<contract>/run_id=…/`.

Hive partitioning is `contract_id=<name>/year=<YYYY>/month=<MM>/`
per `docs/SPEC-warehouse-app.md` §4.1.

Slice 1 scope: identity + lifecycle + provenance columns. Stage
durations and phase durations land in later slices when the contract
defines stages (Slice 4+).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from .contract import Contract
from .service import make_github_source, make_jira_source
from .sources.intervals import (
    github_workitem_to_transitions,
    jira_workitem_to_transitions,
)


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    contract_id: str
    started_at: datetime
    completed_at: datetime
    items_fetched: int


def materialise(
    *,
    contract: Contract,
    data_dir: Path,
    cache_dir: Path,
    offline: bool,
) -> RunManifest:
    """Run one ETL pass for `contract`. Returns the manifest written
    to disk so the caller (CLI) can echo summary stats.
    """
    started_at = datetime.now(UTC)
    run_id = uuid.uuid4().hex[:16]

    if contract.source == "github":
        assert contract.repo  # validated at load_contract
        # Slice 1 sources are read-only when --offline is set; live fetch
        # is the cron path. Item-id prefix conventions handled by
        # `service.make_github_source`.
        source = make_github_source(
            contract.repo,
            cache_dir=cache_dir,
            read_only=offline,
        )
    else:
        assert contract.jira_url and contract.jira_project
        source = make_jira_source(
            contract.jira_url,
            contract.jira_project,
            cache_dir=cache_dir,
            read_only=offline,
        )

    assert contract.start and contract.stop, (
        "Slice 1 contract requires explicit start/stop dates; "
        "richer scoping arrives in later slices."
    )
    items = list(source.fetch_completed_in_window(contract.start, contract.stop))

    completed_at = datetime.now(UTC)

    # Partition layout: contract_id/year/month/day. The date comes from
    # the ETL run (materialised_at), so each cron tick lands in its own
    # daily partition. Re-runs within the same calendar day overwrite
    # that day's file (atomic via .tmp + rename); cron-once-a-day gives
    # one Parquet per contract per day, so you can read history as a
    # date glob if you ever need to.
    year = completed_at.year
    month = completed_at.month
    day = completed_at.day
    work_items_path = (
        data_dir
        / "work_items"
        / f"contract_id={contract.name}"
        / f"year={year}"
        / f"month={month:02d}"
        / f"day={day:02d}"
        / "items.parquet"
    )
    transitions_path = (
        data_dir
        / "transitions"
        / f"contract_id={contract.name}"
        / f"year={year}"
        / f"month={month:02d}"
        / f"day={day:02d}"
        / "transitions.parquet"
    )
    work_items_path.parent.mkdir(parents=True, exist_ok=True)
    transitions_path.parent.mkdir(parents=True, exist_ok=True)

    _write_work_items_parquet(
        items=items,
        contract=contract,
        run_id=run_id,
        materialised_at=completed_at,
        out_path=work_items_path,
    )

    # Dispatch on contract.source — we know what produced these items
    # and don't need to sniff item_id prefixes (the bridge's prefix-
    # based dispatch isn't accurate against real GitHub item ids like
    # `#19342`).
    to_txs = (
        github_workitem_to_transitions
        if contract.source == "github"
        else jira_workitem_to_transitions
    )
    transitions = []
    for item in items:
        transitions.extend(to_txs(item))
    _write_transitions_parquet(
        transitions=transitions,
        contract_source=contract.source,
        contract_id=contract.name,
        out_path=transitions_path,
    )

    manifest = RunManifest(
        run_id=run_id,
        contract_id=contract.name,
        started_at=started_at,
        completed_at=completed_at,
        items_fetched=len(items),
    )
    _write_manifest(manifest=manifest, data_dir=data_dir)
    return manifest


# ---------------------------------------------------------------------------
# Parquet writers
# ---------------------------------------------------------------------------

_WORK_ITEMS_DDL = """
CREATE TEMPORARY TABLE work_items (
    source              VARCHAR,
    repo                VARCHAR,
    item_id             VARCHAR,
    title               VARCHAR,
    url                 VARCHAR,
    author              VARCHAR,
    is_bot              BOOLEAN,
    created_at          TIMESTAMP,
    completed_at        TIMESTAMP,
    cycle_time_days     DOUBLE,
    contract_id         VARCHAR,
    materialised_at     TIMESTAMP,
    run_id              VARCHAR
)
"""

_TRANSITIONS_DDL = """
CREATE TEMPORARY TABLE transitions (
    source              VARCHAR,
    item_id             VARCHAR,
    entered_at          TIMESTAMP,
    stage               VARCHAR,
    signal              VARCHAR,
    contract_id         VARCHAR
)
"""


def _work_item_row(item, contract: Contract, run_id: str, materialised_at: datetime):
    cycle_days = None
    if item.completed_at is not None:
        cycle_days = (item.completed_at - item.created_at).total_seconds() / 86400.0
    return (
        contract.source,
        contract.repo,
        item.item_id,
        item.title,
        item.url,
        item.author_login,
        bool(item.is_bot),
        item.created_at,
        item.completed_at,
        cycle_days,
        contract.name,
        materialised_at,
        run_id,
    )


def _write_work_items_parquet(
    *,
    items,
    contract: Contract,
    run_id: str,
    materialised_at: datetime,
    out_path: Path,
) -> None:
    """Atomic Parquet write: build TEMP TABLE, COPY TO `.tmp`, rename.

    Atomic rename means concurrent readers either see the previous
    snapshot or the new one — never a torn half-written file.
    """
    rows = [_work_item_row(i, contract, run_id, materialised_at) for i in items]
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    con = duckdb.connect()
    try:
        con.execute(_WORK_ITEMS_DDL)
        if rows:
            con.executemany(
                "INSERT INTO work_items VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
        # Use parameterised path? DuckDB COPY TO needs a literal — escape
        # single quotes defensively even though our tmp_path is
        # tool-generated.
        path_literal = str(tmp_path).replace("'", "''")
        con.execute(
            f"COPY work_items TO '{path_literal}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    os.replace(tmp_path, out_path)


def _write_transitions_parquet(
    *,
    transitions,
    contract_source: str,
    contract_id: str,
    out_path: Path,
) -> None:
    # `source` comes from the contract — we already dispatched on it
    # to build the transitions, so the column is constant per call.
    rows = [
        (
            contract_source,
            t.item_id,
            t.entered_at,
            t.stage,
            t.signal,
            contract_id,
        )
        for t in transitions
    ]
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    con = duckdb.connect()
    try:
        con.execute(_TRANSITIONS_DDL)
        if rows:
            con.executemany(
                "INSERT INTO transitions VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        path_literal = str(tmp_path).replace("'", "''")
        con.execute(
            f"COPY transitions TO '{path_literal}' (FORMAT PARQUET)"
        )
    finally:
        con.close()
    os.replace(tmp_path, out_path)


def _write_manifest(*, manifest: RunManifest, data_dir: Path) -> None:
    out_dir = (
        data_dir / "runs" / manifest.contract_id / f"run_id={manifest.run_id}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": manifest.run_id,
        "contract_id": manifest.contract_id,
        "started_at": manifest.started_at.isoformat(),
        "completed_at": manifest.completed_at.isoformat(),
        "items_fetched": manifest.items_fetched,
    }
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2))
