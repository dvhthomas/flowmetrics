"""`flow materialize-all` — iterate every YAML in `--workflows-dir`,
run materialize per workflow, write a daily JSON manifest with
per-workflow outcomes.

The scheduler templates (cron / launchd / Task Scheduler) point at
this one command so the user only schedules one invocation. The
manifest is what monitoring reads; exit code is what schedulers read.

Workflow:
  - Success per workflow → record in manifest with status=ok.
  - Failure per workflow → record with status=failed + error message;
    the next workflow still runs.
  - Exit 0 if at least one workflow succeeded; non-zero only if
    every workflow failed (so a single-bad-YAML doesn't page on-call).
  - Manifest path: `<data-dir>/_status/daily-<UTC-date>.json` by default,
    overridable with --manifest.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


def _write_contract(contracts_dir: Path, name: str, repo: str) -> None:
    (contracts_dir / f"{name}.yaml").write_text(yaml.safe_dump({
        "workflow": {
            "name": name, "source": "github", "repo": repo,
            "start": "2026-05-04", "stop": "2026-05-10",
        }
    }))


@pytest.fixture
def workspace(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    data = tmp_path / "data"
    return contracts, data


class TestEmpty:
    def test_no_contracts_writes_an_empty_manifest_and_exits_clean(self, workspace):
        contracts, data = workspace
        res = CliRunner().invoke(cli, [
            "materialize", "--all",
            "--workflows-dir", str(contracts),
            "--data-dir", str(data),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ], catch_exceptions=False)
        # Empty workflow-dir is not a failure; the cron job just had
        # nothing to do today. Operators read the manifest, not the
        # exit code, to learn this.
        assert res.exit_code == 0, res.output
        manifests = list((data / "_status").glob("daily-*.json"))
        assert len(manifests) == 1
        m = json.loads(manifests[0].read_text())
        assert m["schema"] == "flowmetrics.materialize_all.v1"
        assert m["results"] == []


class TestHappyPath:
    def test_runs_each_contract_and_records_status(self, workspace):
        contracts, data = workspace
        _write_contract(contracts, "astral-uv-week", "astral-sh/uv")
        res = CliRunner().invoke(cli, [
            "materialize", "--all",
            "--workflows-dir", str(contracts),
            "--data-dir", str(data),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        m = json.loads(
            next((data / "_status").glob("daily-*.json")).read_text()
        )
        assert len(m["results"]) == 1
        only = m["results"][0]
        assert only["workflow"] == "astral-uv-week"
        assert only["status"] == "ok"
        # The manifest's date stamp uses UTC, not the host TZ.
        stamp = m["started_at"]
        assert stamp.endswith("+00:00") or stamp.endswith("Z")


class TestMixedOutcomes:
    def test_unparseable_yaml_is_skipped_at_import_time(self, workspace):
        """A YAML that fails parse never enters the DB (per the C1
        migration semantics); materialize-all therefore doesn't
        attempt to run it. The good workflow still processes; the
        bad YAML is left in the dir for the user to fix."""
        contracts, data = workspace
        _write_contract(contracts, "astral-uv-week", "astral-sh/uv")
        # Intentionally broken — no `source:`.
        (contracts / "broken.yaml").write_text("workflow: {name: broken}\n")
        res = CliRunner().invoke(cli, [
            "materialize", "--all",
            "--workflows-dir", str(contracts),
            "--data-dir", str(data),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ], catch_exceptions=False)
        assert res.exit_code == 0
        m = json.loads(
            next((data / "_status").glob("daily-*.json")).read_text()
        )
        statuses = {r["workflow"]: r["status"] for r in m["results"]}
        assert statuses["astral-uv-week"] == "ok"
        # The broken YAML does NOT appear in the manifest — it was
        # never imported into the DB.
        assert "broken" not in statuses
        # The broken YAML is still on disk for the user to fix.
        assert (contracts / "broken.yaml").exists()
        # The good workflow still wrote Parquet despite the bad one.
        assert (data / "work_items").exists()

    def test_all_materialize_failures_exit_non_zero(self, workspace):
        """When every DB-row workflow's materialize call fails (e.g.
        cache miss in offline mode), the exit code is non-zero so
        on-call gets paged. Contrast with the empty-dir case
        (no rows → exit 0)."""
        contracts, data = workspace
        # Valid YAML but the cache won't carry data for these
        # synthetic repos → materialize() raises (offline+miss).
        _write_contract(contracts, "alpha-only", "no-such/repo-alpha")
        _write_contract(contracts, "beta-only", "no-such/repo-beta")
        res = CliRunner().invoke(cli, [
            "materialize", "--all",
            "--workflows-dir", str(contracts),
            "--data-dir", str(data),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ], catch_exceptions=False)
        assert res.exit_code != 0
        manifest_path = next((data / "_status").glob("daily-*.json"))
        m = json.loads(manifest_path.read_text())
        assert m["results"]  # not empty
        assert all(r["status"] == "failed" for r in m["results"])


class TestManifestPathOverride:
    def test_manifest_flag_chooses_the_output(self, workspace, tmp_path):
        contracts, data = workspace
        _write_contract(contracts, "astral-uv-week", "astral-sh/uv")
        out = tmp_path / "custom-manifest.json"
        res = CliRunner().invoke(cli, [
            "materialize", "--all",
            "--workflows-dir", str(contracts),
            "--data-dir", str(data),
            "--cache-dir", str(FIXTURE_CACHE),
            "--manifest", str(out),
            "--offline",
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert out.exists()
        # The default `_status/daily-<date>.json` is NOT also written
        # when --manifest is provided (single source of truth).
        assert not (data / "_status").exists() or not list(
            (data / "_status").glob("daily-*.json")
        )


class TestDateStamping:
    def test_default_manifest_filename_uses_utc_date(self, workspace, monkeypatch):
        contracts, data = workspace
        _write_contract(contracts, "astral-uv-week", "astral-sh/uv")
        # Pin "now" to a known UTC instant — the manifest filename
        # must echo this date, not the host's local-TZ date.
        from flowmetrics import cli as cli_mod
        pinned = datetime(2026, 5, 26, 23, 30, tzinfo=UTC)
        monkeypatch.setattr(
            cli_mod, "_materialize_all_now", lambda: pinned, raising=False,
        )
        res = CliRunner().invoke(cli, [
            "materialize", "--all",
            "--workflows-dir", str(contracts),
            "--data-dir", str(data),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert (data / "_status" / "daily-2026-05-26.json").exists()
