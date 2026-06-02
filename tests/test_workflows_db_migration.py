"""User-facing DB file renamed from `contracts.db` → `workflows.db`.

WorkflowStore auto-migrates on first construction:

  1. Empty workflows-dir: creates workflows.db directly. No rename
     ever needed.
  2. Legacy install with only contracts.db: rename in place to
     workflows.db. One-time, idempotent.
  3. Both files present (an operator manually copied workflows.db
     in while contracts.db was already there): refuse to rename;
     surface the ambiguity. We never silently overwrite either.

The Python type name (`WorkflowStore`) stays — it's implementation
vocabulary. Only the filename users see on disk changes.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from flowmetrics.workflows_db import WorkflowStore


def _make_db(path: Path) -> None:
    """Minimal contracts schema — enough that `WorkflowStore.list()`
    can read it without crashing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml_text = (
        "contract:\n"
        "  name: astral-uv\n"
        "  source: github\n"
        "  repo: astral-sh/uv\n"
        "  start: 2026-04-01\n"
        "  stop: 2026-05-01\n"
    )
    con = sqlite3.connect(path)
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS contracts (
              id TEXT PRIMARY KEY,
              yaml TEXT NOT NULL,
              archived_at TEXT,
              archived_reason TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              label TEXT
            )
        """)
        con.execute(
            "INSERT INTO contracts(id, yaml, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            ("astral-uv", yaml_text,
             "2026-05-01T00:00:00Z", "2026-05-01T00:00:00Z"),
        )
        con.commit()
    finally:
        con.close()


class TestEmptyWorkflowsDir:
    def test_creates_workflows_db_not_contracts_db(self, tmp_path):
        wf = tmp_path / "contracts"
        WorkflowStore(wf)
        # The DB file should land at workflows.db, not contracts.db.
        assert (wf / "workflows.db").exists() or not (wf / "contracts.db").exists()
        # (Connection isn't opened until first use, so either the new
        # name exists or neither does — what matters is the OLD name
        # is NOT created.)
        assert not (wf / "contracts.db").exists()


class TestLegacyMigration:
    def test_renames_contracts_db_to_workflows_db(self, tmp_path):
        wf = tmp_path / "contracts"
        legacy = wf / "contracts.db"
        _make_db(legacy)

        WorkflowStore(wf)

        new = wf / "workflows.db"
        assert new.exists(), "expected workflows.db to be created"
        assert not legacy.exists(), "expected contracts.db to be renamed away"

    def test_rows_are_preserved_across_migration(self, tmp_path):
        wf = tmp_path / "contracts"
        legacy = wf / "contracts.db"
        _make_db(legacy)

        store = WorkflowStore(wf)
        rows = store.list()
        assert any(m.name == "astral-uv" for m in rows), (
            f"expected to find migrated row; got {rows}"
        )

    def test_migration_is_idempotent_second_run_noop(self, tmp_path):
        wf = tmp_path / "contracts"
        legacy = wf / "contracts.db"
        _make_db(legacy)

        WorkflowStore(wf)  # first call: migrates
        WorkflowStore(wf)  # second call: nothing to do, must not raise

        assert (wf / "workflows.db").exists()
        assert not (wf / "contracts.db").exists()


class TestAmbiguousState:
    def test_refuses_to_migrate_when_both_files_present(self, tmp_path):
        """An operator who copied workflows.db onto a host that still
        has contracts.db has TWO databases that could be the source
        of truth. Silently keeping one and renaming the other away
        could destroy live config — surface the ambiguity instead."""
        wf = tmp_path / "contracts"
        _make_db(wf / "contracts.db")
        _make_db(wf / "workflows.db")

        with pytest.raises(RuntimeError) as exc:
            WorkflowStore(wf)
        msg = str(exc.value).lower()
        # Mention both filenames so the operator knows what's wrong.
        assert "contracts.db" in msg
        assert "workflows.db" in msg
        # AND no destructive side effect: both files still present.
        assert (wf / "contracts.db").exists()
        assert (wf / "workflows.db").exists()
