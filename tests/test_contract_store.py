"""`WorkflowStore` — the single contract-persistence adapter.

Both the web app and the CLI go through this so the "YAML vs DB
read/write" decision lives in one place:
  - writes go to the SQLite store;
  - reads resolve DB-first, then fall back to a YAML file on disk
    (a contract dropped in but not yet migrated) — without moving it;
  - `ensure_initialized()` is the explicit, idempotent migration that
    imports leftover YAMLs into the DB (the only thing that moves files).
"""

from __future__ import annotations

import pytest

from flowmetrics.workflow import parse_workflow_text
from flowmetrics.workflows_db import WorkflowStore


def _yaml(name: str, repo: str = "o/r") -> str:
    return f"contract:\n  name: {name}\n  source: github\n  repo: {repo}\n"


def _c(name: str, repo: str = "o/r"):
    return parse_workflow_text(_yaml(name, repo), name)


@pytest.fixture
def store(tmp_path):
    return WorkflowStore(tmp_path / "wf")


class TestReadsResolveDbFirstThenYaml:
    def test_put_then_read_from_db(self, store):
        store.put(_c("alpha"))
        assert store.get("alpha").name == "alpha"
        assert store.get_meta("alpha").name == "alpha"

    def test_get_falls_back_to_a_yaml_on_disk_without_moving_it(self, tmp_path):
        wf = tmp_path / "wf"
        wf.mkdir()
        (wf / "beta.yaml").write_text(_yaml("beta"))
        store = WorkflowStore(wf)
        # Not in the DB, but the YAML resolves it...
        assert store.get("beta").name == "beta"
        assert store.get_meta("beta").name == "beta"
        # ...and the read does NOT migrate/move the file.
        assert (wf / "beta.yaml").exists()
        assert not (wf / "migrated").exists()

    def test_db_row_wins_over_a_yaml_of_the_same_name(self, tmp_path):
        wf = tmp_path / "wf"
        wf.mkdir()
        (wf / "gamma.yaml").write_text(_yaml("gamma", repo="yaml/repo"))
        store = WorkflowStore(wf)
        store.put(_c("gamma", repo="db/repo"))
        assert store.get("gamma").repo == "db/repo"

    def test_missing_everywhere_is_none(self, store):
        assert store.get("nope") is None
        assert store.get_meta("nope") is None


class TestMigration:
    def test_ensure_initialized_imports_yaml_into_the_db(self, tmp_path):
        wf = tmp_path / "wf"
        wf.mkdir()
        (wf / "delta.yaml").write_text(_yaml("delta"))
        store = WorkflowStore(wf)
        store.ensure_initialized()
        assert "delta" in [m.name for m in store.list()]
        # The migration moves the file aside (rollback copy).
        assert (wf / "migrated" / "delta.yaml").exists()


class TestWrites:
    def test_archive_hides_from_list_then_restore_brings_it_back(self, store):
        store.put(_c("eps"))
        store.archive("eps", reason="done")
        assert "eps" not in [m.name for m in store.list()]
        assert "eps" in [m.name for m in store.list(include_archived=True)]
        store.restore("eps")
        assert "eps" in [m.name for m in store.list()]

    def test_hard_delete_after_archive(self, store):
        store.put(_c("zed"))
        store.archive("zed")
        store.hard_delete("zed")
        assert store.get("zed") is None
