"""SQLite-backed contract store.

The DB at `<workflows-dir>/contracts.db` is server-managed: a single
`contracts` table with columns `(id, yaml, archived_at,
archived_reason, created_at, updated_at)`. The body of the contract
lives in the `yaml` column as canonical YAML text — adding a future
field to the Contract Pydantic model needs zero DB migration.

`ensure_initialized(workflows_dir)` is the first-boot hook: it
creates the DB, scans the workflows dir for legacy `*.yaml` /
`*.yml` files, imports each one as a row, and moves the file to
`<workflows-dir>/migrated/` so the user has a rollback. Subsequent
runs are idempotent — already-migrated files are gone; new YAMLs
dropped in later get imported on the next call. Existing DB rows
are never clobbered by a re-imported YAML (the API row wins).
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .contract import (
    Contract,
    ContractError,
    emit_canonical_yaml,
    parse_contract_text,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contracts (
  id TEXT PRIMARY KEY,
  yaml TEXT NOT NULL,
  archived_at TEXT,
  archived_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS contracts_archived_at_idx
  ON contracts(archived_at);
"""


class ContractsDBError(Exception):
    """Raised by ContractsDB on operations the caller can recover
    from (live-row delete attempt, restore-collision, etc.)."""


@dataclass(frozen=True)
class ContractMeta:
    """A row's wrapper carrying lifecycle metadata alongside the
    parsed Contract object."""

    contract: Contract
    yaml: str                # canonical YAML text as stored
    created_at: str
    updated_at: str
    archived_at: str | None
    archived_reason: str | None

    @property
    def name(self) -> str:
        """Routing-id shortcut so callers that just want the id can
        write `meta.name` instead of `meta.contract.name`."""
        return self.contract.name


class ContractsDB:
    """Thin wrapper over the `contracts` SQLite table.

    Connections are short-lived (one per method call) — the volume
    is small (~dozens of rows in practice) and SQLite's default
    locking is fine for the single-process Uvicorn worker.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as con:
            con.executescript(_SCHEMA)

    # ------------------------------------------------------------ helpers

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def _row_to_meta(self, row: sqlite3.Row) -> ContractMeta:
        contract = parse_contract_text(row["yaml"], row["id"])
        return ContractMeta(
            contract=contract,
            yaml=row["yaml"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            archived_at=row["archived_at"],
            archived_reason=row["archived_reason"],
        )

    # ------------------------------------------------------------ CRUD

    def put(self, contract: Contract) -> None:
        """Upsert the contract.

          - New id → INSERT live row (created_at = updated_at = now).
          - Existing LIVE id → UPDATE (keep created_at, bump
            updated_at).
          - Existing ARCHIVED id → REFUSE with a clear error.
            The user wants a fresh contract with that name;
            archive + new-with-same-id collision needs an explicit
            decision (restore the old or hard-delete it first), not
            silent resurrection.
        """
        yaml_text = emit_canonical_yaml(contract)
        now = self._now()
        with self._connect() as con:
            existing = con.execute(
                "SELECT created_at, archived_at FROM contracts "
                "WHERE id = ?",
                (contract.name,),
            ).fetchone()
            if existing is None:
                con.execute(
                    "INSERT INTO contracts "
                    "(id, yaml, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (contract.name, yaml_text, now, now),
                )
            elif existing["archived_at"] is not None:
                raise ContractsDBError(
                    f"a contract with id {contract.name!r} is "
                    "archived. Restore it (or hard-delete it) "
                    "before creating a new contract with the "
                    "same id."
                )
            else:
                con.execute(
                    "UPDATE contracts SET yaml = ?, updated_at = ? "
                    "WHERE id = ?",
                    (yaml_text, now, contract.name),
                )

    def get(self, contract_id: str) -> Contract | None:
        meta = self.get_meta(contract_id)
        return meta.contract if meta else None

    def get_meta(self, contract_id: str) -> ContractMeta | None:
        with self._connect() as con:
            row = con.execute(
                "SELECT * FROM contracts WHERE id = ?", (contract_id,)
            ).fetchone()
            return self._row_to_meta(row) if row else None

    def list(self, *, include_archived: bool = False) -> list[ContractMeta]:
        with self._connect() as con:
            if include_archived:
                rows = con.execute(
                    "SELECT * FROM contracts ORDER BY id"
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM contracts "
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
                "SELECT archived_at FROM contracts WHERE id = ?",
                (contract_id,),
            ).fetchone()
            if row is None:
                raise ContractsDBError(
                    f"contract {contract_id!r} not found"
                )
            if row["archived_at"] is None:
                con.execute(
                    "UPDATE contracts SET archived_at = ?, "
                    "archived_reason = ?, updated_at = ? WHERE id = ?",
                    (self._now(), reason, self._now(), contract_id),
                )
            else:
                # Idempotent: only the reason rolls forward.
                con.execute(
                    "UPDATE contracts SET archived_reason = ? "
                    "WHERE id = ?",
                    (reason, contract_id),
                )

    def restore(self, contract_id: str) -> None:
        """Clears `archived_at`. Refuses if a LIVE contract with the
        same id exists — that'd shadow the row and confuse listings."""
        with self._connect() as con:
            row = con.execute(
                "SELECT archived_at FROM contracts WHERE id = ?",
                (contract_id,),
            ).fetchone()
            if row is None:
                raise ContractsDBError(
                    f"contract {contract_id!r} not found"
                )
            if row["archived_at"] is None:
                return  # already live; nothing to do
            # Is there another live contract with the same id? In
            # current schema id is PRIMARY KEY so no — but a future
            # schema (e.g. versioned ids) might allow it. The
            # invariant is documented here: refuse if any LIVE
            # contract with this id exists, ignoring the archived
            # row we're about to restore.
            collide = con.execute(
                "SELECT 1 FROM contracts "
                "WHERE id = ? AND archived_at IS NULL",
                (contract_id,),
            ).fetchone()
            if collide is not None:
                raise ContractsDBError(
                    f"a live contract with id {contract_id!r} "
                    "already exists; rename it before restoring."
                )
            con.execute(
                "UPDATE contracts SET archived_at = NULL, "
                "archived_reason = NULL, updated_at = ? WHERE id = ?",
                (self._now(), contract_id),
            )

    def hard_delete(self, contract_id: str) -> None:
        """Permanently delete a row. Refuses unless the contract is
        already archived (the two-step delete invariant)."""
        with self._connect() as con:
            row = con.execute(
                "SELECT archived_at FROM contracts WHERE id = ?",
                (contract_id,),
            ).fetchone()
            if row is None:
                raise ContractsDBError(
                    f"contract {contract_id!r} not found"
                )
            if row["archived_at"] is None:
                raise ContractsDBError(
                    f"contract {contract_id!r} is live; archive it "
                    "first before hard-deleting."
                )
            con.execute(
                "DELETE FROM contracts WHERE id = ?", (contract_id,)
            )


# ---------------------------------------------------------------------------
# First-boot migration: import legacy YAMLs into the DB.
# ---------------------------------------------------------------------------


def ensure_initialized(workflows_dir: Path) -> None:
    """Idempotent first-boot migration.

    1. Creates the DB at `<workflows-dir>/contracts.db` if missing.
    2. For every `*.yaml` / `*.yml` in the workflows dir (top-level
       only — `migrated/` is intentionally excluded), parse the
       YAML, INSERT it into the DB (skipping ids that already exist
       — the DB wins), then move the file into `migrated/`.
    3. A YAML that fails to parse is left in place so the user can
       see it and fix it; subsequent calls retry.
    """
    workflows_dir = Path(workflows_dir)
    workflows_dir.mkdir(parents=True, exist_ok=True)
    db = ContractsDB(workflows_dir / "contracts.db")
    migrated_dir = workflows_dir / "migrated"

    for path in sorted(workflows_dir.iterdir()):
        if not path.is_file():
            continue
        if path.suffix not in (".yaml", ".yml"):
            continue
        if path.name == "contracts.db":
            continue
        name = path.stem
        try:
            contract = parse_contract_text(path.read_text(), name)
        except ContractError:
            # Leave the bad YAML where it is so the user sees it.
            continue
        # DB wins — if the row already exists, don't clobber it.
        if db.get(name) is None:
            db.put(contract)
        migrated_dir.mkdir(exist_ok=True)
        shutil.move(str(path), str(migrated_dir / path.name))
