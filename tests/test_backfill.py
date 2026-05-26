"""Tests for backfill status — the per-workflow JSON file a
browser-triggered `flow materialise` writes so the Data Source
page can poll progress.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import yaml
from click.testing import CliRunner

from flowmetrics.backfill import (
    display_status,
    is_active,
    read_status,
    status_path,
    write_status,
)
from flowmetrics.cli import cli

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


class TestStatusFileHelpers:
    def test_status_path_is_per_workflow_under_data_dir(self):
        p = status_path(Path("/data"), "apache-cassandra-week")
        assert p == Path("/data/_status/apache-cassandra-week.json")

    def test_write_then_read_round_trips(self, tmp_path):
        p = status_path(tmp_path, "wf")
        write_status(p, {"workflow": "wf", "status": "running"})
        assert read_status(p) == {"workflow": "wf", "status": "running"}

    def test_read_missing_file_is_none(self, tmp_path):
        assert read_status(tmp_path / "absent.json") is None

    def test_write_leaves_no_tmp_file_behind(self, tmp_path):
        """Atomic write (tmp + rename) — a poll mid-write must
        never see a half-written file or a stray .tmp."""
        p = status_path(tmp_path, "wf")
        write_status(p, {"status": "done"})
        assert list(p.parent.glob("*.tmp")) == []


def _contracts_dir() -> tuple[Path, Path]:
    """A temp dir with one `astral-uv-week` GitHub contract."""
    tmp = Path(tempfile.mkdtemp())
    contracts = tmp / "contracts"
    contracts.mkdir()
    (contracts / "astral-uv-week.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
                    "name": "astral-uv-week",
                    "source": "github",
                    "repo": "astral-sh/uv",
                    "start": "2026-05-04",
                    "stop": "2026-05-10",
                }
            }
        )
    )
    return tmp, contracts


class TestMaterialiseStatusFile:
    def test_records_done_on_success(self):
        """`flow materialise --status-file` writes a `done` record
        with the lifecycle timestamps + a summary message."""
        tmp, contracts = _contracts_dir()
        status = tmp / "status.json"
        res = CliRunner().invoke(
            cli,
            [
                "materialise", "astral-uv-week",
                "--data-dir", str(tmp / "data"),
                "--workflows-dir", str(contracts),
                "--cache-dir", str(FIXTURE_CACHE),
                "--offline",
                "--status-file", str(status),
            ],
            catch_exceptions=False,
        )
        assert res.exit_code == 0, res.output
        s = read_status(status)
        assert s is not None
        assert s["status"] == "done"
        assert s["workflow"] == "astral-uv-week"
        assert s["started_at"] and s["finished_at"]
        assert s["message"], "done record must carry a summary"

    def test_records_failed_on_bad_contract(self):
        """A failed run leaves a `failed` record naming the error
        — the page shows it instead of a silent dead poll."""
        tmp, contracts = _contracts_dir()
        status = tmp / "status.json"
        res = CliRunner().invoke(
            cli,
            [
                "materialise", "does-not-exist",
                "--data-dir", str(tmp / "data"),
                "--workflows-dir", str(contracts),
                "--cache-dir", str(FIXTURE_CACHE),
                "--offline",
                "--status-file", str(status),
            ],
        )
        assert res.exit_code != 0
        s = read_status(status)
        assert s is not None
        assert s["status"] == "failed"
        assert s["message"], "failed record must name the error"

    def test_without_status_file_writes_nothing(self):
        """`--status-file` is opt-in — a plain cron materialise
        writes no status file."""
        tmp, contracts = _contracts_dir()
        res = CliRunner().invoke(
            cli,
            [
                "materialise", "astral-uv-week",
                "--data-dir", str(tmp / "data"),
                "--workflows-dir", str(contracts),
                "--cache-dir", str(FIXTURE_CACHE),
                "--offline",
            ],
            catch_exceptions=False,
        )
        assert res.exit_code == 0, res.output
        assert not (tmp / "data" / "_status").exists()


class TestStaleLock:
    """A `running` record from a crashed subprocess must not wedge
    a workflow forever — past STALE_AFTER it stops counting as an
    active backfill, and the UI surfaces it as failed."""

    def _running(self, started_at: str) -> dict:
        return {
            "workflow": "w", "status": "running",
            "started_at": started_at, "finished_at": None,
            "message": "",
        }

    def test_recent_running_is_active(self):
        now = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
        s = self._running(
            datetime(2026, 5, 21, 11, 58, tzinfo=UTC).isoformat()
        )
        assert is_active(s, now) is True

    def test_stale_running_is_not_active(self):
        """Older than STALE_AFTER → the lock releases."""
        now = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
        s = self._running(
            datetime(2026, 5, 21, 11, 0, tzinfo=UTC).isoformat()
        )
        assert is_active(s, now) is False

    def test_done_and_none_are_not_active(self):
        now = datetime.now(UTC)
        assert is_active({"status": "done"}, now) is False
        assert is_active(None, now) is False

    def test_display_status_coerces_stale_running_to_failed(self):
        now = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
        s = self._running(
            datetime(2026, 5, 21, 11, 0, tzinfo=UTC).isoformat()
        )
        shown = display_status(s, now)
        assert shown["status"] == "failed"
        assert shown["message"], "stale → failed must explain itself"

    def test_display_status_leaves_a_live_running_alone(self):
        now = datetime(2026, 5, 21, 12, 0, tzinfo=UTC)
        s = self._running(
            datetime(2026, 5, 21, 11, 59, tzinfo=UTC).isoformat()
        )
        assert display_status(s, now)["status"] == "running"


class TestMaterialiseSnapshotsAreAdditive:
    def test_two_same_day_runs_write_two_files(self):
        """Two materialise runs on the same calendar day each
        write their own Parquet (the filename carries the run_id)
        — a backfill never overwrites a same-day snapshot."""
        tmp, contracts = _contracts_dir()
        data = tmp / "data"
        for _ in range(2):
            res = CliRunner().invoke(
                cli,
                [
                    "materialise", "astral-uv-week",
                    "--data-dir", str(data),
                    "--workflows-dir", str(contracts),
                    "--cache-dir", str(FIXTURE_CACHE),
                    "--offline",
                ],
                catch_exceptions=False,
            )
            assert res.exit_code == 0, res.output
        work_items = list((data / "work_items").rglob("*.parquet"))
        transitions = list((data / "transitions").rglob("*.parquet"))
        assert len(work_items) == 2, (
            f"two runs must leave two work_items files; got {work_items}"
        )
        assert len(transitions) == 2, (
            f"two runs must leave two transitions files; got {transitions}"
        )


class TestMaterialiseSweepsStaleTmp:
    def test_materialise_removes_a_stale_tmp_on_start(self):
        """A `.tmp` left by a prior interrupted write is swept when
        the next materialise runs — the data dir stays rsync-tidy."""
        tmp, contracts = _contracts_dir()
        data = tmp / "data"
        stale = data / "work_items" / "items-crashed.parquet.tmp"
        stale.parent.mkdir(parents=True)
        stale.write_text("half-written debris")
        old = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
        os.utime(stale, (old, old))

        res = CliRunner().invoke(
            cli,
            [
                "materialise", "astral-uv-week",
                "--data-dir", str(data),
                "--workflows-dir", str(contracts),
                "--cache-dir", str(FIXTURE_CACHE),
                "--offline",
            ],
            catch_exceptions=False,
        )
        assert res.exit_code == 0, res.output
        assert not stale.exists(), (
            "a stale .tmp must be swept when materialise runs"
        )
        # The real snapshots are written and intact.
        assert list((data / "work_items").rglob("*.parquet"))


class TestCompaction:
    """Compaction collapses the accumulated snapshot files into one
    per table — every work item kept at its latest version, only
    the redundant older snapshots dropped."""

    def _materialise(self, data, contracts):
        return CliRunner().invoke(
            cli,
            [
                "materialise", "astral-uv-week",
                "--data-dir", str(data),
                "--workflows-dir", str(contracts),
                "--cache-dir", str(FIXTURE_CACHE),
                "--offline",
            ],
            catch_exceptions=False,
        )

    def test_compaction_collapses_files_but_preserves_the_data(self):
        """The deduped warehouse the read view sees is byte-for-byte
        identical before and after compaction — nothing lost."""
        from flowmetrics.app import open_warehouse
        from flowmetrics.materialise import compact_contract

        tmp, contracts = _contracts_dir()
        data = tmp / "data"
        for _ in range(2):
            assert self._materialise(data, contracts).exit_code == 0
        assert len(list((data / "work_items").rglob("*.parquet"))) == 2

        con = open_warehouse(data)
        before = con.execute(
            "SELECT item_id, materialised_at, completed_at "
            "FROM work_items ORDER BY item_id"
        ).fetchall()
        before_tx = con.execute(
            "SELECT count(*) FROM transitions"
        ).fetchone()[0]
        con.close()

        compact_contract(data, "astral-uv-week", now=datetime.now(UTC))

        assert len(list((data / "work_items").rglob("*.parquet"))) == 1, (
            "compaction collapses work_items to a single file"
        )
        con = open_warehouse(data)
        after = con.execute(
            "SELECT item_id, materialised_at, completed_at "
            "FROM work_items ORDER BY item_id"
        ).fetchall()
        after_tx = con.execute(
            "SELECT count(*) FROM transitions"
        ).fetchone()[0]
        con.close()
        assert after == before, "compaction must preserve every work item"
        assert after_tx == before_tx, "compaction must preserve transitions"

    def test_materialise_keeps_the_file_count_bounded(self):
        """Materialise compacts before each run, so snapshot files
        don't grow without bound — three runs leave two files, not
        three."""
        tmp, contracts = _contracts_dir()
        data = tmp / "data"
        for _ in range(3):
            assert self._materialise(data, contracts).exit_code == 0
        files = list((data / "work_items").rglob("*.parquet"))
        assert len(files) == 2, (
            f"compaction should bound files to ~2; got {files}"
        )


class TestBackfillProgressFragment:
    """The progress fragment refreshes the whole Data Source page
    when a backfill finishes — so the coverage chart and headline
    pick up the new data — but ONLY when the fragment was delivered
    by the 2s poll. A page that loads on an already-`done` status
    must NOT reload, or it would loop forever."""

    @staticmethod
    def _render(ctx: dict) -> str:
        from jinja2 import Environment, FileSystemLoader

        import flowmetrics

        templates_dir = (
            Path(flowmetrics.__file__).parent / "web" / "templates"
        )
        env = Environment(loader=FileSystemLoader(str(templates_dir)))
        return env.get_template(
            "_partials/backfill_progress.html.jinja"
        ).render(**ctx)

    _DONE: ClassVar = {
        "status": "done", "message": "", "since": None, "until": None,
    }
    _RUNNING: ClassVar = {
        "status": "running", "message": "",
        "since": "2026-05-04", "until": "2026-05-10",
    }

    def test_poll_delivered_done_fragment_reloads_the_page(self):
        html = self._render(
            {"backfill": self._DONE, "workflow": "demo", "poll": True}
        )
        assert "location.reload" in html

    def test_page_load_done_fragment_does_not_reload(self):
        # No `poll` flag — this is the page-load render of an
        # already-finished backfill. Reloading here would loop.
        html = self._render({"backfill": self._DONE, "workflow": "demo"})
        assert "location.reload" not in html

    def test_running_fragment_polls_and_does_not_reload(self):
        html = self._render(
            {"backfill": self._RUNNING, "workflow": "demo", "poll": True}
        )
        # Still polling for status; not reloading.
        assert "backfill-status" in html
        assert "location.reload" not in html
