"""C1 — first-boot migration: import legacy `<workflows-dir>/*.yaml`
files into the SQLite store, then move them to
`<workflows-dir>/migrated/` so the rollback path exists.

Triggered automatically on `flow serve` / `flow materialize(-all)`
first-run, and available via `flow contracts migrate` for cron-style
installs. Idempotent — subsequent runs with no YAMLs are no-ops.
"""

from __future__ import annotations

from pathlib import Path

import yaml


def _write_yaml(dirpath: Path, name: str, **fields) -> None:
    payload = {"name": name, "source": "github", "repo": "owner/repo"}
    payload.update(fields)
    (dirpath / f"{name}.yaml").write_text(yaml.safe_dump({"contract": payload}))


class TestEnsureInitialized:
    def test_creates_db_and_imports_every_yaml(self, tmp_path):
        from flowmetrics.workflows_db import WorkflowsDB, ensure_initialized
        workflows = tmp_path / "workflows"
        workflows.mkdir()
        _write_yaml(workflows, "alpha", label="Alpha")
        _write_yaml(workflows, "beta", source="jira",
                    jira_url="https://j.example.com", jira_project="B",
                    repo=None)

        ensure_initialized(workflows)

        db = WorkflowsDB(workflows / "workflows.db")
        ids = {c.name for c in db.list()}
        assert ids == {"alpha", "beta"}

    def test_processed_yamls_move_to_migrated_subdir(self, tmp_path):
        from flowmetrics.workflows_db import ensure_initialized
        workflows = tmp_path / "workflows"
        workflows.mkdir()
        _write_yaml(workflows, "alpha")
        _write_yaml(workflows, "beta")

        ensure_initialized(workflows)

        # The YAMLs are gone from the top level but recoverable under
        # migrated/ — the rollback path.
        assert not (workflows / "alpha.yaml").exists()
        assert not (workflows / "beta.yaml").exists()
        assert (workflows / "migrated" / "alpha.yaml").exists()
        assert (workflows / "migrated" / "beta.yaml").exists()

    def test_idempotent_when_no_yamls_present(self, tmp_path):
        from flowmetrics.workflows_db import WorkflowsDB, ensure_initialized
        workflows = tmp_path / "workflows"
        workflows.mkdir()
        # Run once with one YAML.
        _write_yaml(workflows, "alpha")
        ensure_initialized(workflows)
        # Run a second time — no YAMLs in the top-level dir now.
        ensure_initialized(workflows)
        # DB unchanged, no errors.
        db = WorkflowsDB(workflows / "workflows.db")
        assert [c.name for c in db.list()] == ["alpha"]

    def test_yaml_in_dir_after_first_run_imports_on_re_run(self, tmp_path):
        from flowmetrics.workflows_db import WorkflowsDB, ensure_initialized
        workflows = tmp_path / "workflows"
        workflows.mkdir()
        _write_yaml(workflows, "alpha")
        ensure_initialized(workflows)
        # User drops a new YAML in later. Next call picks it up.
        _write_yaml(workflows, "gamma")
        ensure_initialized(workflows)
        db = WorkflowsDB(workflows / "workflows.db")
        assert {c.name for c in db.list()} == {"alpha", "gamma"}

    def test_existing_row_not_overwritten_if_yaml_returns(self, tmp_path):
        """If someone restores a YAML out of `migrated/` and the
        ID matches a row already in the DB, the existing row wins
        (the DB is the source of truth). The YAML still moves so
        the user knows it was processed."""
        from flowmetrics.workflows_db import WorkflowsDB, ensure_initialized
        workflows = tmp_path / "workflows"
        workflows.mkdir()
        _write_yaml(workflows, "alpha", label="from-yaml")
        ensure_initialized(workflows)
        db = WorkflowsDB(workflows / "workflows.db")
        # User edits the DB row through the API (simulated here).
        from flowmetrics.workflow import Contract
        c = db.get("alpha")
        db.put(Contract(**{**c.model_dump(), "label": "from-api"}))
        # User restores the original YAML.
        _write_yaml(workflows, "alpha", label="from-yaml")
        ensure_initialized(workflows)
        # The API value wins; YAML didn't clobber the DB.
        assert db.get("alpha").label == "from-api"
        # And the re-imported YAML moves anyway.
        assert not (workflows / "alpha.yaml").exists()

    def test_malformed_yaml_does_not_crash(self, tmp_path):
        """A broken YAML in the dir is skipped (left in place so the
        user can fix it); ensure_initialized returns normally."""
        from flowmetrics.workflows_db import WorkflowsDB, ensure_initialized
        workflows = tmp_path / "workflows"
        workflows.mkdir()
        _write_yaml(workflows, "good")
        # Missing source: → contract parse fails.
        (workflows / "bad.yaml").write_text("contract: {name: bad}\n")
        ensure_initialized(workflows)
        db = WorkflowsDB(workflows / "workflows.db")
        assert [c.name for c in db.list()] == ["good"]
        # Bad YAML left where it was so the user can see it.
        assert (workflows / "bad.yaml").exists()
        assert not (workflows / "migrated" / "bad.yaml").exists()
