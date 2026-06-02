"""SQLite-backed workflow store.

The DB at `<workflows-dir>/contracts.db` is server-managed: a single
`contracts` table with columns `(id, yaml, archived_at,
archived_reason, created_at, updated_at)`. The body of the workflow
lives in the `yaml` column as canonical YAML text — adding a future
field to the Workflow Pydantic model needs zero DB migration.

`ensure_initialized(workflows_dir)` is the first-boot hook: it
creates the DB, scans the workflows dir for legacy `*.yaml` /
`*.yml` files, imports each one as a row, and moves the file to
`<workflows-dir>/migrated/` so the user has a rollback. Subsequent
runs are idempotent — already-migrated files are gone; new YAMLs
dropped in later get imported on the next call. Existing DB rows
are never clobbered by a re-imported YAML (the API row wins).
"""

from __future__ import annotations

import contextlib
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .workflow import (
    Workflow,
    WorkflowError,
    emit_canonical_yaml,
    parse_workflow_text,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows (
  id TEXT PRIMARY KEY,
  yaml TEXT NOT NULL,
  archived_at TEXT,
  archived_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS workflows_archived_at_idx
  ON workflows(archived_at);
"""


def _migrate_legacy_table(con: sqlite3.Connection) -> None:
    """One-time rename of the legacy `contracts` table → `workflows`.

    Existing installs predating the SQL rename have a workflows.db
    file (the filename rename shipped earlier) that still contains
    a `contracts` table inside. Detect that shape on open and ALTER
    TABLE in place so a live install survives the upgrade with no
    operator action.

    Idempotent: a fresh DB has only `workflows` and this is a no-op.
    Defensive: BOTH tables present is operator-induced ambiguity
    (typically: someone copied an old DB onto a new one); raise so
    we never silently pick one.
    """
    rows = con.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name IN ('contracts', 'workflows')"
    ).fetchall()
    names = {r[0] for r in rows}
    if "contracts" in names and "workflows" in names:
        raise WorkflowsDBError(
            "ambiguous DB state: both `contracts` and `workflows` "
            "tables exist in this file. Resolve manually (typically: "
            "keep the one with the rows you want, drop the other)."
        )
    if "contracts" in names and "workflows" not in names:
        con.execute("ALTER TABLE contracts RENAME TO workflows")
        # Rename the index to follow the table; older DBs might not
        # have one (IF NOT EXISTS in the schema) — the subsequent
        # CREATE INDEX in _SCHEMA will create one with the new name.
        with contextlib.suppress(sqlite3.OperationalError):
            con.execute(
                "ALTER INDEX contracts_archived_at_idx "
                "RENAME TO workflows_archived_at_idx"
            )


class WorkflowsDBError(Exception):
    """Raised by WorkflowsDB on operations the caller can recover
    from (live-row delete attempt, restore-collision, etc.)."""


@dataclass(frozen=True)
class WorkflowMeta:
    """A row's wrapper carrying lifecycle metadata alongside the
    parsed Workflow object."""

    workflow: Workflow
    yaml: str                # canonical YAML text as stored
    created_at: str
    updated_at: str
    archived_at: str | None
    archived_reason: str | None

    @property
    def name(self) -> str:
        """Routing-id shortcut so callers that just want the id can
        write `meta.name` instead of `meta.workflow.name`."""
        return self.workflow.name


class WorkflowsDB:
    """Thin wrapper over the `contracts` SQLite table.

    Connections are short-lived (one per method call) — the volume
    is small (~dozens of rows in practice) and SQLite's default
    locking is fine for the single-process Uvicorn worker.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            _migrate_legacy_table(con)
            con.executescript(_SCHEMA)

    # ------------------------------------------------------------ helpers

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _row_to_meta(self, row: sqlite3.Row) -> WorkflowMeta:
        workflow = parse_workflow_text(row["yaml"], row["id"])
        return WorkflowMeta(
            workflow=workflow,
            yaml=row["yaml"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
            archived_reason=row["archived_reason"],
        )

    # ------------------------------------------------------------ CRUD

    def put(self, workflow: Workflow) -> None:
        """Upsert the workflow.

          - New id → INSERT live row (created_at = updated_at = now).
          - Existing LIVE id → UPDATE (keep created_at, bump
            updated_at).
          - Existing ARCHIVED id → REFUSE with a clear error.
            The user wants a fresh workflow with that name;
            archive + new-with-same-id collision needs an explicit
            decision (restore the old or hard-delete it first), not
            silent resurrection.
        """
        yaml_text = emit_canonical_yaml(workflow)
        now = self._now()
        with self._connect() as con:
            existing = con.execute(
                "SELECT created_at, archived_at FROM workflows "
                "WHERE id = ?",
                (workflow.name,),
            ).fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO workflows "
                    "(id, yaml, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (workflow.name, yaml_text, now, now),
                )
            elif existing["archived_at"] is not None:
                raise WorkflowsDBError(
                    f"a workflow with id {workflow.name!r} is "
                    "archived. Restore it (or hard-delete it) "
                    "before creating a new workflow with the "
                    "same id."
                )
            else:
                con.execute(
                    "UPDATE workflows SET yaml = ?, updated_at = ? "
                    "WHERE id = ?",
                    (yaml_text, now, workflow.name),
                )

    def get(self, contract_id: str) -> Workflow | None:
        meta = self.get_meta(contract_id)
        return meta.workflow if meta else None

    def get_meta(self, contract_id: str) -> WorkflowMeta | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM workflows WHERE id = ?", (contract_id,)
            ).fetchone()
            return self._row_to_meta(row) if row else None

    def list(self, *, include_archived: bool = False) -> list[WorkflowMeta]:
        with self._connect() as con:
            if include_archived:
                rows = con.execute(
                    "SELECT * FROM workflows ORDER BY id"
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM workflows "
                    "WHERE archived_at IS NULL ORDER BY id"
                ).fetchall()
        return [self._row_to_meta(r) for r in rows]

    # ------------------------------------------------------------ lifecycle

    def archive(self, contract_id: str, *, reason: str | None = None) -> None:
        """Sets `archived_at` (idempotent — re-archiving keeps the
        original timestamp, but DOES update the reason so the audit
        captures the latest call)."""
        with self._connect() as con:
            row = con.execute(
                "SELECT archived_at FROM workflows WHERE id = ?",
                (contract_id,),
            ).fetchone()
            if row is None:
                raise WorkflowsDBError(
                    f"workflow {contract_id!r} not found"
                )
            if row["archived_at"] is None:
                con.execute(
                    "UPDATE workflows SET archived_at = ?, "
                    "archived_reason = ?, updated_at = ? WHERE id = ?",
                    (self._now(), reason, self._now(), contract_id),
                )
            else:
                # Idempotent: only the reason rolls forward.
                con.execute(
                    "UPDATE workflows SET archived_reason = ? "
                    "WHERE id = ?",
                    (reason, contract_id),
                )

    def restore(self, contract_id: str) -> None:
        """Clears `archived_at`. Refuses if a LIVE workflow with the
        same id exists — that'd shadow the row and confuse listings."""
        with self._connect() as con:
            row = con.execute(
                "SELECT archived_at FROM workflows WHERE id = ?",
                (contract_id,),
            ).fetchone()
            if row is None:
                raise WorkflowsDBError(
                    f"workflow {contract_id!r} not found"
                )
            if row["archived_at"] is None:
                return  # already live; nothing to do
            # Is there another live workflow with the same id? In
            # current schema id is PRIMARY KEY so no — but a future
            # schema (e.g. versioned ids) might allow it. The
            # invariant is documented here: refuse if any LIVE
            # workflow with this id exists, ignoring the archived
            # row we're about to restore.
            collide = con.execute(
                "SELECT 1 FROM workflows "
                "WHERE id = ? AND archived_at IS NULL",
                (contract_id,),
            ).fetchone()
            if collide is not None:
                raise WorkflowsDBError(
                    f"a live workflow with id {contract_id!r} "
                    "already exists; rename it before restoring."
                )
            con.execute(
                "UPDATE workflows SET archived_at = NULL, "
                "archived_reason = NULL, updated_at = ? WHERE id = ?",
                (self._now(), contract_id),
            )

    def hard_delete(self, contract_id: str) -> None:
        """Permanently delete a row. Refuses unless the workflow is
        already archived (the two-step delete invariant)."""
        with self._connect() as con:
            row = con.execute(
                "SELECT archived_at FROM workflows WHERE id = ?",
                (contract_id,),
            ).fetchone()
            if row is None:
                raise WorkflowsDBError(
                    f"workflow {contract_id!r} not found"
                )
            if row["archived_at"] is None:
                raise WorkflowsDBError(
                    f"workflow {contract_id!r} is live; archive it "
                    "first before hard-deleting."
                )
            con.execute(
                "DELETE FROM workflows WHERE id = ?", (contract_id,)
            )


# ---------------------------------------------------------------------------
# DB filename + one-time legacy rename.
# ---------------------------------------------------------------------------

_LEGACY_DB_FILENAME = "contracts.db"
_DB_FILENAME = "workflows.db"


def _resolve_db_path(workflows_dir: Path) -> Path:
    """Return the path to the SQLite store, performing a one-time
    rename from the legacy `contracts.db` filename if needed.

    Behaviour:
      - both `workflows.db` and `contracts.db` exist: ambiguous, raise.
      - only `contracts.db` exists: os.replace it to `workflows.db`.
      - only `workflows.db` exists, or neither: nothing to do.
    """
    legacy = workflows_dir / _LEGACY_DB_FILENAME
    new = workflows_dir / _DB_FILENAME
    if legacy.exists() and new.exists():
        raise RuntimeError(
            f"both {legacy} and {new} are present — this is ambiguous, "
            "we don't know which is the source of truth. Move or "
            "delete one and try again (workflows.db is the new "
            "canonical name; contracts.db is the legacy name)."
        )
    if legacy.exists() and not new.exists():
        # os.replace is atomic on the same filesystem — survives a
        # crash mid-rename without leaving the user with neither file.
        import os as _os
        _os.replace(str(legacy), str(new))
    return new


# ---------------------------------------------------------------------------
# First-boot migration: import legacy YAMLs into the DB.
# ---------------------------------------------------------------------------


def ensure_initialized(workflows_dir: Path) -> None:
    """Idempotent first-boot migration.

    1. Renames legacy `contracts.db` → `workflows.db` if applicable.
    2. Creates the DB at `<workflows-dir>/workflows.db` if missing.
    3. For every `*.yaml` / `*.yml` in the workflows dir (top-level
       only — `migrated/` is intentionally excluded), parse the
       YAML, INSERT it into the DB (skipping ids that already exist
       — the DB wins), then move the file into `migrated/`.
    4. A YAML that fails to parse is left in place so the user can
       see it and fix it; subsequent calls retry.
    """
    workflows_dir = Path(workflows_dir)
    workflows_dir.mkdir(parents=True, exist_ok=True)
    db = WorkflowsDB(_resolve_db_path(workflows_dir))
    migrated_dir = workflows_dir / "migrated"

    for path in sorted(workflows_dir.iterdir()):
        if not path.is_file():
            continue
        if path.suffix not in (".yaml", ".yml"):
            continue
        if path.name in (_LEGACY_DB_FILENAME, _DB_FILENAME):
            continue
        name = path.stem
        try:
            workflow = parse_workflow_text(path.read_text(encoding="utf-8"), name)
        except WorkflowError:
            # Leave the bad YAML where it is so the user sees it.
            continue
        # DB wins — if the row already exists, don't clobber it.
        if db.get(name) is None:
            db.put(workflow)
        migrated_dir.mkdir(exist_ok=True)
        shutil.move(str(path), str(migrated_dir / path.name))


# ---------------------------------------------------------------------------
# WorkflowStore — the single persistence adapter (DB + YAML fallback).
# ---------------------------------------------------------------------------


class WorkflowStore:
    """The one workflow-persistence adapter for both the web app and
    the CLI, so the "YAML vs DB read/write" decision lives in exactly
    one place.

    Backed by the SQLite store at `<workflows_dir>/workflows.db` plus
    the YAML files in that directory:

      - **writes** (put / archive / restore / hard_delete) go to the DB;
      - **reads** (get / get_meta) resolve DB-first, then fall back to a
        YAML file on disk — a workflow dropped in but not yet migrated —
        *without moving it* (a read has no side effects);
      - `ensure_initialized()` is the explicit, idempotent migration that
        imports leftover YAMLs into the DB. It is the only operation that
        moves files.

    First-construction also performs a one-time rename of any legacy
    `contracts.db` to `workflows.db` (the new canonical name).
    """

    def __init__(self, workflows_dir: Path) -> None:
        self.workflows_dir = Path(workflows_dir)
        # Migration runs even when the dir doesn't exist yet — _resolve
        # is a no-op when there's nothing to rename.
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        db_path = _resolve_db_path(self.workflows_dir)
        self.db = WorkflowsDB(db_path)

    # -------------------------------------------------------- migration

    def ensure_initialized(self) -> None:
        ensure_initialized(self.workflows_dir)

    # ------------------------------------------------------------ reads

    def get_meta(self, contract_id: str) -> WorkflowMeta | None:
        meta = self.db.get_meta(contract_id)
        return meta if meta is not None else self._yaml_meta(contract_id)

    def get(self, contract_id: str) -> Workflow | None:
        meta = self.get_meta(contract_id)
        return meta.workflow if meta is not None else None

    def list(self, *, include_archived: bool = False) -> list[WorkflowMeta]:
        return self.db.list(include_archived=include_archived)

    # ----------------------------------------------------------- writes

    def put(self, workflow: Workflow) -> None:
        self.db.put(workflow)

    def archive(self, contract_id: str, *, reason: str | None = None) -> None:
        self.db.archive(contract_id, reason=reason)

    def restore(self, contract_id: str) -> None:
        self.db.restore(contract_id)

    def hard_delete(self, contract_id: str) -> None:
        self.db.hard_delete(contract_id)

    # --------------------------------------------------------- internal

    def _yaml_meta(self, contract_id: str) -> WorkflowMeta | None:
        """Resolve a workflow from a YAML file on disk (read-only). The
        file is never moved — that's `ensure_initialized`'s job."""
        for ext in (".yaml", ".yml"):
            path = self.workflows_dir / f"{contract_id}{ext}"
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            try:
                workflow = parse_workflow_text(text, contract_id)
            except WorkflowError:
                return None
            stamp = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
            return WorkflowMeta(
                workflow=workflow,
                yaml=text,
                created_at=stamp,
                updated_at=stamp,
                archived_at=None,
                archived_reason=None,
            )
        return None
