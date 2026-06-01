"""`flow workflows list` — read-only enumeration of configured
workflows.

The materialize commands silently read from contracts.db (DB-first)
with a fallback to un-migrated YAMLs in the workflows-dir. Until this
command existed, the only way to see what was configured was to open
the web UI. That asymmetry was a discoverability bug — agents and
operators driving from the CLI had to guess.

The command pins three behaviours:

  1. Empty workflows-dir → helpful "no workflows" message + the
     wizard URL (`flow serve` then http://...). NOT a stack trace.
  2. DB rows render with `source: db`; un-migrated YAML files
     render with `source: yaml`. Same name in both → DB wins (the
     web UI's first-boot migration would resolve this anyway, but
     the CLI should be honest about what wins).
  3. Archived contracts hidden by default; `--all` includes them
     with an `[archived]` suffix.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from click.testing import CliRunner

from flowmetrics.cli import cli


def _make_yaml(workflows_dir: Path, name: str, *, repo: str = "owner/repo") -> None:
    workflows_dir.mkdir(parents=True, exist_ok=True)
    (workflows_dir / f"{name}.yaml").write_text(
        f"""
contract:
  name: {name}
  source: github
  repo: {repo}
  start: 2026-04-01
  stop:  2026-05-01
""".strip()
    )


def _write_db_row(
    workflows_dir: Path,
    *,
    name: str,
    repo: str = "owner/repo",
    archived: bool = False,
) -> None:
    """Write a row directly to contracts.db without going through the
    serve startup. Lets us pin DB-source behaviour without spinning
    up the wizard."""
    workflows_dir.mkdir(parents=True, exist_ok=True)
    db = workflows_dir / "contracts.db"
    yaml = (
        f"contract:\n"
        f"  name: {name}\n"
        f"  source: github\n"
        f"  repo: {repo}\n"
        f"  start: 2026-04-01\n"
        f"  stop: 2026-05-01\n"
    )
    con = sqlite3.connect(db)
    try:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS contracts (
              id TEXT PRIMARY KEY,
              yaml TEXT NOT NULL,
              archived_at TEXT,
              archived_reason TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL,
              label TEXT
            );
            """
        )
        con.execute(
            "INSERT INTO contracts(id, yaml, archived_at, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                name,
                yaml,
                "2026-05-30T00:00:00Z" if archived else None,
                "2026-05-01T00:00:00Z",
                "2026-05-01T00:00:00Z",
            ),
        )
        con.commit()
    finally:
        con.close()


class TestEmpty:
    def test_empty_workflows_dir_shows_helpful_message(self, tmp_path):
        result = CliRunner().invoke(
            cli,
            ["workflows", "list", "--workflows-dir", str(tmp_path / "contracts")],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        assert "no workflows" in out
        # Point the operator at the wizard, since that's the path
        # of least resistance to actually configuring one.
        assert "serve" in out or "new workflow" in out


class TestListsDbRows:
    def test_shows_active_db_rows_with_source_marker(self, tmp_path):
        wf = tmp_path / "contracts"
        _write_db_row(wf, name="astral-uv", repo="astral-sh/uv")
        _write_db_row(wf, name="kno-shaping", repo="dvhthomas/kno")

        result = CliRunner().invoke(
            cli,
            ["workflows", "list", "--workflows-dir", str(wf)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "astral-uv" in result.output
        assert "kno-shaping" in result.output
        assert "astral-sh/uv" in result.output
        assert "dvhthomas/kno" in result.output
        # Each row carries a source marker so the operator can
        # distinguish wizard-managed (db) from check-in-able (yaml).
        assert "db" in result.output.lower()


class TestListsYamlFallback:
    def test_shows_unmigrated_yaml_files_with_source_marker(self, tmp_path):
        """A user can drop a YAML into the workflows-dir without
        having run `flow serve` (which is what migrates YAMLs into
        the DB). `flow workflows list` must still see them so
        `flow materialize NAME` against a YAML-only contract is
        discoverable."""
        wf = tmp_path / "contracts"
        _make_yaml(wf, "scripted-workflow")

        result = CliRunner().invoke(
            cli,
            ["workflows", "list", "--workflows-dir", str(wf)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "scripted-workflow" in result.output
        assert "yaml" in result.output.lower()


class TestDbWinsWhenBothPresent:
    def test_db_row_shadows_yaml_with_same_name(self, tmp_path):
        """If the same id has both a DB row and an un-migrated
        YAML, the DB row is authoritative — matches the resolution
        order ContractStore.get() uses."""
        wf = tmp_path / "contracts"
        _make_yaml(wf, "same-name", repo="from-yaml/repo")
        _write_db_row(wf, name="same-name", repo="from-db/repo")

        result = CliRunner().invoke(
            cli,
            ["workflows", "list", "--workflows-dir", str(wf)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        # The DB row is what materialize would use; surface that.
        assert "from-db/repo" in result.output
        assert "from-yaml/repo" not in result.output
        # And `same-name` appears once, not twice.
        assert result.output.count("same-name") == 1


class TestArchived:
    def test_archived_hidden_by_default(self, tmp_path):
        wf = tmp_path / "contracts"
        _write_db_row(wf, name="active", repo="owner/active")
        _write_db_row(wf, name="retired", repo="owner/retired", archived=True)

        result = CliRunner().invoke(
            cli,
            ["workflows", "list", "--workflows-dir", str(wf)],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "active" in result.output
        assert "retired" not in result.output

    def test_all_flag_includes_archived_with_marker(self, tmp_path):
        wf = tmp_path / "contracts"
        _write_db_row(wf, name="active", repo="owner/active")
        _write_db_row(wf, name="retired", repo="owner/retired", archived=True)

        result = CliRunner().invoke(
            cli,
            ["workflows", "list", "--workflows-dir", str(wf), "--all"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0, result.output
        assert "active" in result.output
        assert "retired" in result.output
        # Archived rows are clearly marked so a quick glance doesn't
        # confuse them with active ones.
        assert "archived" in result.output.lower()
