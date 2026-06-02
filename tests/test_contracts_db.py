"""C1 — yaml-in-a-column SQLite store for contracts.

The DB at `<workflows-dir>/contracts.db` is server-managed: a single
`contracts` table with columns `(id, yaml, archived_at,
archived_reason, created_at, updated_at)`. The DB owns lifecycle
metadata; the YAML text owns shape (Pydantic validates writes).

Tests pin:
  - CRUD round-trip via the canonical Contract objects.
  - Listing excludes archived rows by default.
  - Archive sets the timestamp + reason; restore clears it.
  - Hard delete refuses on a live row; succeeds on an archived row.
  - Timestamps: created_at on insert, updated_at on update.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from flowmetrics.workflows_db import WorkflowsDB, WorkflowsDBError


@pytest.fixture
def db(tmp_path):
    return WorkflowsDB(tmp_path / "contracts.db")


def _c(name="x", source="github", repo="a/b", steps=None, **kw):
    from flowmetrics.workflow import Contract, Step
    return Contract(
        name=name, source=source, repo=repo,
        steps=[Step(**s) for s in (steps or [])],
        **kw,
    )


class TestInitialization:
    def test_first_open_creates_the_schema(self, tmp_path):
        from flowmetrics.workflows_db import WorkflowsDB
        path = tmp_path / "contracts.db"
        assert not path.exists()
        WorkflowsDB(path)
        assert path.exists()
        con = sqlite3.connect(path)
        try:
            cur = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='contracts'"
            )
            assert cur.fetchone() is not None
        finally:
            con.close()


class TestCrud:
    def test_insert_then_get_round_trips(self, db):
        c = _c(name="alpha", repo="acme/foo")
        db.put(c)
        loaded = db.get("alpha")
        assert loaded == c

    def test_get_missing_returns_none(self, db):
        assert db.get("nope") is None

    def test_list_excludes_archived_by_default(self, db):
        db.put(_c(name="a"))
        db.put(_c(name="b"))
        db.archive("b")
        ids = [c.name for c in db.list()]
        assert ids == ["a"]

    def test_list_include_archived_returns_both_with_flag(self, db):
        db.put(_c(name="a"))
        db.put(_c(name="b"))
        db.archive("b")
        rows = db.list(include_archived=True)
        by_id = {r.contract.name: r for r in rows}
        assert by_id["a"].archived_at is None
        assert by_id["b"].archived_at is not None

    def test_put_existing_id_updates(self, db):
        db.put(_c(name="x", label="old"))
        db.put(_c(name="x", label="new"))
        assert db.get("x").label == "new"


class TestArchiveLifecycle:
    def test_archive_sets_timestamp_and_reason(self, db):
        db.put(_c(name="x"))
        db.archive("x", reason="rotating out")
        row = db.get_meta("x")
        assert row.archived_at is not None
        assert row.archived_reason == "rotating out"

    def test_restore_clears_archive_state(self, db):
        db.put(_c(name="x"))
        db.archive("x", reason="oops")
        db.restore("x")
        row = db.get_meta("x")
        assert row.archived_at is None
        assert row.archived_reason is None

    def test_archive_is_idempotent(self, db):
        db.put(_c(name="x"))
        db.archive("x", reason="first")
        first = db.get_meta("x").archived_at
        db.archive("x", reason="second")
        # idempotent — the timestamp doesn't move on a second call.
        assert db.get_meta("x").archived_at == first
        # reason DOES update, so the audit captures the latest call.
        assert db.get_meta("x").archived_reason == "second"

    def test_hard_delete_refuses_on_live_contract(self, db):
        db.put(_c(name="x"))
        with pytest.raises(WorkflowsDBError):
            db.hard_delete("x")
        # Still there.
        assert db.get("x") is not None

    def test_hard_delete_succeeds_when_archived(self, db):
        db.put(_c(name="x"))
        db.archive("x")
        db.hard_delete("x")
        assert db.get("x") is None
        # Even ?include_archived=true can't see it.
        assert not any(r.contract.name == "x" for r in db.list(include_archived=True))

    def test_put_refuses_when_id_is_archived(self, db):
        # Archive "x", then try to create a NEW contract with id "x".
        # The DB refuses with an explicit error so the user has to
        # decide: restore the old, or hard-delete it first. No
        # silent resurrection.
        db.put(_c(name="x", label="first"))
        db.archive("x")
        with pytest.raises(WorkflowsDBError):
            db.put(_c(name="x", label="second"))


class TestTimestamps:
    def test_created_at_set_on_insert(self, db):
        db.put(_c(name="x"))
        m = db.get_meta("x")
        assert m.created_at is not None
        assert m.updated_at == m.created_at

    def test_updated_at_bumps_on_update(self, db):
        db.put(_c(name="x", label="v1"))
        original = db.get_meta("x")
        time.sleep(0.01)  # ensure clock resolution
        db.put(_c(name="x", label="v2"))
        latest = db.get_meta("x")
        assert latest.created_at == original.created_at
        assert latest.updated_at > original.updated_at
