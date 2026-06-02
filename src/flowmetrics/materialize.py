"""ETL: workflow → source fetch → canonical → Parquet.

Invoked by `flow materialize NAME`. One-shot:

  1. Load workflow YAML.
  2. Build source adapter (GitHub or Jira) using existing factories.
  3. Fetch completed items in the workflow's window.
  4. Convert each WorkItem → fact-table row (work_items.parquet).
  5. Convert each WorkItem.status_intervals → StageTransition rows
     (transitions.parquet).
  6. Write Parquet atomically (write `.tmp`, rename).
  7. Append a run manifest under `data/runs/<workflow>/run_id=…/`.

Hive partitioning is `contract_id=<name>/year=<YYYY>/month=<MM>/`
per `docs/SPEC-warehouse-app.md` §4.1.

Slice 1 scope: identity + lifecycle + provenance columns. Stage
durations and phase durations land in later slices when the workflow
defines stages (Slice 4+).
"""

from __future__ import annotations

import json
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb

from .matching import remap_transitions
from .service import make_github_source, make_jira_source
from .sources.intervals import (
    github_workitem_to_transitions,
    jira_workitem_to_transitions,
)
from .workflow import Workflow

# A `.tmp` file from a Parquet/status write is short-lived — it
# exists only for the few seconds of the `COPY`, then is renamed
# into place. Anything older than this is debris from an
# interrupted write and is safe to delete; a fresh `.tmp` from a
# write in flight is always younger and is never touched.
_STALE_TMP_AFTER = timedelta(minutes=30)

# A workflow may omit start/stop (the web builder no longer asks for a
# window — data is fetched via the Data Source page's backfill, which
# passes its own range). When neither is set, fetch the most recent
# window of this many days up to today so the scheduled import still has
# a bounded, sensible default rather than the source's full history.
DEFAULT_FETCH_WINDOW_DAYS = 90


def _resolve_window(workflow: Workflow, *, today: date) -> tuple[date, date]:
    """The fetch window for this run: the workflow's explicit start/stop
    when present, otherwise a rolling `DEFAULT_FETCH_WINDOW_DAYS` window
    ending today. Each bound defaults independently."""
    stop = workflow.stop or today
    start = workflow.start or (stop - timedelta(days=DEFAULT_FETCH_WINDOW_DAYS))
    return start, stop


def cleanup_tmp_files(
    data_dir: Path,
    *,
    now: datetime,
    older_than: timedelta = _STALE_TMP_AFTER,
) -> int:
    """Delete stale `.tmp` debris left by interrupted Parquet or
    status writes, so the data directory stays clean (and
    rsync-tidy). Returns the number deleted.

    ONLY `.tmp` files are ever removed, and only those older than
    `older_than` — `.parquet` snapshots and `.yaml` configs are
    never touched, and a `.tmp` from a write in flight is spared.
    """
    deleted = 0
    root = Path(data_dir)
    if not root.exists():
        return 0
    for tmp in root.rglob("*.tmp"):
        try:
            mtime = datetime.fromtimestamp(tmp.stat().st_mtime, tz=UTC)
        except OSError:
            continue
        if now - mtime >= older_than:
            with suppress(OSError):
                tmp.unlink()
                deleted += 1
    return deleted


@dataclass(frozen=True)
class RunManifest:
    run_id: str
    contract_id: str
    started_at: datetime
    completed_at: datetime
    items_fetched: int


def _compact_one(
    *, contract_dir: Path, stem: str, dedup_sql: str, now: datetime,
) -> None:
    """Collapse one table's snapshot files for a workflow into a
    single file. `dedup_sql` runs against a `src` view of all the
    originals and must reproduce the read view's dedup exactly.

    Crash-safe: write the compacted file (atomic .tmp + rename),
    THEN delete the originals. A crash leaves either {originals}
    or {originals + compacted} — both dedup to the same result.
    """
    if not contract_dir.exists():
        return
    originals = sorted(contract_dir.rglob(f"{stem}*.parquet"))
    if len(originals) < 2:
        return  # already a single file (or none) — nothing to merge
    out_dir = (
        contract_dir
        / f"year={now.year}"
        / f"month={now.month:02d}"
        / f"day={now.day:02d}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{stem}-compacted-{uuid.uuid4().hex[:16]}.parquet"
    tmp_path = out_path.parent / f"{out_path.name}.tmp"
    file_list = ", ".join(
        "'" + str(p).replace("'", "''") + "'" for p in originals
    )
    con = duckdb.connect()
    try:
        # Pin TZ=UTC so reads of tz-aware values from older snapshots
        # (or future TIMESTAMPTZ columns) don't shift by the host's
        # offset on the way through compaction. [[feedback_flowmetrics_anchor_is_authoritative]]
        con.execute("SET TimeZone='UTC'")
        # No hive_partitioning: read the pure data columns so the
        # compacted file's schema matches a normal snapshot —
        # year/month/day stay path-only, never baked in as columns.
        # union_by_name tolerates a schema bump across snapshots (e.g.
        # transitions gaining materialized_at/run_id); older files get
        # NULL for the new columns and are superseded at dedup time.
        con.execute(
            f"CREATE VIEW src AS SELECT * FROM read_parquet("
            f"[{file_list}], union_by_name = true)"
        )
        out_literal = str(tmp_path).replace("'", "''")
        con.execute(f"COPY ({dedup_sql}) TO '{out_literal}' (FORMAT PARQUET)")
    finally:
        con.close()
    os.replace(tmp_path, out_path)
    # Compacted file is safely in place — now drop the originals.
    for p in originals:
        with suppress(OSError):
            p.unlink()


def compact_contract(
    data_dir: Path, contract_name: str, *, now: datetime,
) -> None:
    """Collapse a workflow's accumulated snapshot files into one
    file per table. Reads every snapshot, applies the read view's
    dedup (latest run per work item AND per transition), writes a
    single file, then deletes the originals — never dropping a work
    item, only redundant/superseded older snapshots of it.
    """
    _compact_one(
        contract_dir=(
            data_dir / "work_items" / f"contract_id={contract_name}"
        ),
        stem="items",
        dedup_sql=(
            "SELECT * EXCLUDE (_rn) FROM ("
            " SELECT *, ROW_NUMBER() OVER ("
            "  PARTITION BY contract_id, source, item_id"
            "  ORDER BY materialized_at DESC, run_id DESC) AS _rn"
            " FROM src) WHERE _rn = 1"
        ),
        now=now,
    )
    _compact_one(
        contract_dir=(
            data_dir / "transitions" / f"contract_id={contract_name}"
        ),
        stem="transitions",
        # Keep every transition from the LATEST run per item (DENSE_RANK
        # so all rows sharing the winning (materialized_at, run_id) stay).
        # Must mirror the read view exactly.
        dedup_sql=(
            "SELECT * EXCLUDE (_rr) FROM ("
            " SELECT *, DENSE_RANK() OVER ("
            "  PARTITION BY contract_id, source, item_id"
            "  ORDER BY materialized_at DESC NULLS LAST,"
            "           run_id DESC NULLS LAST) AS _rr"
            " FROM src) WHERE _rr = 1"
        ),
        now=now,
    )


def materialize(
    *,
    workflow: Workflow,
    data_dir: Path,
    cache_dir: Path,
    offline: bool,
) -> RunManifest:
    """Run one ETL pass for `workflow`. Returns the manifest written
    to disk so the caller (CLI) can echo summary stats.
    """
    started_at = datetime.now(UTC)
    run_id = uuid.uuid4().hex[:16]

    # Sweep stale `.tmp` debris from any previously-interrupted
    # write before this run starts. Never touches `.parquet` —
    # the cumulative work-item history is upsert-only and sacred.
    cleanup_tmp_files(data_dir, now=started_at)
    # Collapse accumulated snapshots into one file per table
    # before this run adds its own — keeps the warehouse to ~2
    # files per table without ever dropping a work item.
    compact_contract(data_dir, workflow.name, now=started_at)

    if workflow.source == "github":
        assert workflow.repo  # validated at load_contract
        # Slice 1 sources are read-only when --offline is set; live fetch
        # is the cron path. Item-id prefix conventions handled by
        # `service.make_github_source`.
        source = make_github_source(
            workflow.repo,
            cache_dir=cache_dir,
            read_only=offline,
        )
    else:
        assert workflow.jira_url and workflow.jira_project
        source = make_jira_source(
            workflow.jira_url,
            workflow.jira_project,
            cache_dir=cache_dir,
            read_only=offline,
        )

    # The workflow window is optional; fall back to a rolling default
    # so a UI-built (windowless) workflow still materializes.
    window_start, window_stop = _resolve_window(
        workflow, today=started_at.date()
    )
    completed = list(
        source.fetch_completed_in_window(window_start, window_stop)
    )
    # Also capture in-flight items (started but not yet completed)
    # so the aging-WIP chart has data to plot. Without this the
    # warehouse only knows about completed work and aging is
    # always empty. The asof for fetch_in_flight is "right now"
    # — that's the only honest snapshot of "what's open" the
    # source can give us.
    #
    # Tolerate a cache miss in offline mode: the offline test
    # fixture doesn't carry `is:open` query responses (only
    # `is:merged ... in window`), so re-running an existing
    # offline materialize shouldn't crash. The cost is an empty
    # aging chart until the next online refresh, which is exactly
    # the empty-state UI the user already sees.
    from .cache import CacheMiss

    try:
        in_flight = list(
            source.fetch_in_flight(asof=datetime.now(UTC).date())
        )
    except CacheMiss:
        in_flight = []
    # Defensive de-dup by item_id in case a PR closed between the
    # two fetches and shows up in both lists. The completed copy
    # wins (it has the completion timestamp).
    completed_ids = {i.item_id for i in completed}
    in_flight = [i for i in in_flight if i.item_id not in completed_ids]
    items = completed + in_flight

    completed_at = datetime.now(UTC)

    # Partition layout: contract_id/year/month/day, dated by the
    # ETL run (materialized_at). The FILENAME carries the run_id,
    # so every run — daily cron OR a browser-triggered backfill —
    # writes its own Parquet and never overwrites another. A
    # narrow backfill therefore cannot clobber (or shrink) a
    # broader same-day snapshot. The read view globs every file
    # and dedups by materialized_at.
    year = completed_at.year
    month = completed_at.month
    day = completed_at.day
    work_items_path = (
        data_dir
        / "work_items"
        / f"contract_id={workflow.name}"
        / f"year={year}"
        / f"month={month:02d}"
        / f"day={day:02d}"
        / f"items-{run_id}.parquet"
    )
    transitions_path = (
        data_dir
        / "transitions"
        / f"contract_id={workflow.name}"
        / f"year={year}"
        / f"month={month:02d}"
        / f"day={day:02d}"
        / f"transitions-{run_id}.parquet"
    )
    work_items_path.parent.mkdir(parents=True, exist_ok=True)
    transitions_path.parent.mkdir(parents=True, exist_ok=True)

    _write_work_items_parquet(
        items=items,
        workflow=workflow,
        run_id=run_id,
        materialized_at=completed_at,
        out_path=work_items_path,
    )

    # Dispatch on workflow.source — we know what produced these items
    # and don't need to sniff item_id prefixes (the bridge's prefix-
    # based dispatch isn't accurate against real GitHub item ids like
    # `#19342`).
    to_txs = (
        github_workitem_to_transitions
        if workflow.source == "github"
        else jira_workitem_to_transitions
    )
    transitions = []
    for item in items:
        transitions.extend(to_txs(item))
    # Relabel adapter-native stages to the workflow's step names (the
    # user's workflow). No-op when the workflow defines no steps.
    transitions = remap_transitions(
        transitions, workflow.steps, source=workflow.source
    )
    _write_transitions_parquet(
        transitions=transitions,
        contract_source=workflow.source,
        contract_id=workflow.name,
        out_path=transitions_path,
        materialized_at=completed_at,
        run_id=run_id,
    )

    manifest = RunManifest(
        run_id=run_id,
        contract_id=workflow.name,
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
    materialized_at     TIMESTAMP,
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
    contract_id         VARCHAR,
    materialized_at     TIMESTAMP,
    run_id              VARCHAR
)
"""


def cycle_time_days(
    created_at: datetime, completed_at: datetime | None
) -> float | None:
    """Cycle time in calendar days, using the strict formula:

        CT = FD - SD + 1

    where SD and FD are the UTC calendar dates of `created_at` and
    `completed_at`. Same-day work = 1 day; next-day = 2 days; etc.
    Both endpoints are inclusive — "we'd never say it took zero
    days to complete".

    The result is always integer-valued (stored as float for
    column-type consistency). Sub-day precision is deliberately
    discarded — cycle time is a per-day metric everywhere it's
    used (forecasting, percentile commitments, throughput). The
    raw timestamps remain available on the transitions Parquet so
    the lifecycle / timeline component can still draw an accurate
    second-resolution gantt.

    Returns None for in-flight items. A `completed_at` whose date
    is on-or-before `created_at` produces a non-positive result —
    that's the "bad data" zone (valid minimum is 1.0d). Surfaced
    rather than clamped so source-data corruption is visible.

    See tests/test_calendar_cycle_time.py for the workflow.
    """
    if completed_at is None:
        return None
    sd = created_at.date()
    fd = completed_at.date()
    return float((fd - sd).days + 1)


def _work_item_row(item, workflow: Workflow, run_id: str, materialized_at: datetime):
    cycle_days = cycle_time_days(item.created_at, item.completed_at)
    return (
        workflow.source,
        workflow.repo,
        item.item_id,
        item.title,
        item.url,
        item.author_login,
        bool(item.is_bot),
        item.created_at,
        item.completed_at,
        cycle_days,
        workflow.name,
        materialized_at,
        run_id,
    )


def _write_work_items_parquet(
    *,
    items,
    workflow: Workflow,
    run_id: str,
    materialized_at: datetime,
    out_path: Path,
) -> None:
    """Atomic Parquet write: build TEMP TABLE, COPY TO `.tmp`, rename.

    Atomic rename means concurrent readers either see the previous
    snapshot or the new one — never a torn half-written file.
    """
    rows = [_work_item_row(i, workflow, run_id, materialized_at) for i in items]
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    con = duckdb.connect()
    try:
        # Pin TZ=UTC so tz-aware datetimes flowing into the TIMESTAMP
        # columns are normalised to UTC wall-time before the offset is
        # dropped — without this, the same source data writes
        # different bytes on a Mac (host TZ) than on a UTC CI runner.
        # [[feedback_flowmetrics_anchor_is_authoritative]]
        con.execute("SET TimeZone='UTC'")
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
    materialized_at: datetime,
    run_id: str,
) -> None:
    # `source` comes from the workflow — we already dispatched on it
    # to build the transitions, so the column is constant per call.
    # `materialized_at` + `run_id` stamp the run so the read view can
    # keep only the latest run per item (remap rewrites `stage`, so
    # otherwise stale stage vocab would linger across re-materializes).
    rows = [
        (
            contract_source,
            t.item_id,
            t.entered_at,
            t.stage,
            t.signal,
            contract_id,
            materialized_at,
            run_id,
        )
        for t in transitions
    ]
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    con = duckdb.connect()
    try:
        # See `_write_work_items_parquet` for why we pin TZ=UTC.
        con.execute("SET TimeZone='UTC'")
        con.execute(_TRANSITIONS_DDL)
        if rows:
            con.executemany(
                "INSERT INTO transitions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
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
