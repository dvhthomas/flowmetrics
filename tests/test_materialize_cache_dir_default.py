"""`flow materialize` cache-dir defaults.

When the operator doesn't pass `--cache-dir`, the cache lives under
the `--data-dir` tree (`<data-dir>/.cache/github`). This co-locates
the fetched-from-API substrate with the warehouse it feeds, so:

  - the launchd plist installed by `--bg` never inherits a
    CWD-relative path (which would resolve to `/.cache/github` on
    macOS's sealed-system-volume — read-only, OSError [Errno 30]);
  - operators have ONE tree to back up, prune, or `rm -rf`;
  - cron / systemd / GitHub Actions launchers all get a usable
    default without remembering an extra flag.

Explicit `--cache-dir` still wins for power users.
"""

from __future__ import annotations

import tempfile
from datetime import UTC
from datetime import datetime as _dt
from pathlib import Path
from unittest.mock import patch

import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.materialize import RunManifest


def _make_contracts_dir(tmp: Path) -> Path:
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {
                    "name": "demo",
                    "source": "github",
                    "repo": "astral-sh/uv",
                    "start": "2026-05-04",
                    "stop": "2026-05-10",
                }
            }
        )
    )
    return contracts_dir


def _capture(captured: dict):
    def stub(*, workflow, data_dir, cache_dir, offline):
        captured["workflow"] = workflow
        captured["data_dir"] = data_dir
        captured["cache_dir"] = cache_dir
        captured["offline"] = offline
        return RunManifest(
            run_id="test",
            contract_id=workflow.name,
            started_at=_dt.now(UTC),
            completed_at=_dt.now(UTC),
            items_fetched=0,
        )

    return stub


class TestSingleWorkflowDefault:
    def test_omitted_cache_dir_resolves_under_data_dir(self):
        """`flow materialize demo --data-dir DIR` (no --cache-dir)
        passes `DIR/.cache/github` down to the materialize unit."""
        tmp = Path(tempfile.mkdtemp())
        contracts_dir = _make_contracts_dir(tmp)
        data_dir = tmp / "data"
        captured: dict = {}

        with patch(
            "flowmetrics.materialize.materialize",
            side_effect=_capture(captured),
        ):
            res = CliRunner().invoke(
                cli,
                [
                    "materialize", "demo",
                    "--data-dir", str(data_dir),
                    "--workflows-dir", str(contracts_dir),
                    "--offline",
                ],
                catch_exceptions=False,
            )
        assert res.exit_code == 0, res.output
        assert captured["cache_dir"] == data_dir / ".cache" / "github"

    def test_explicit_cache_dir_overrides_default(self):
        """`--cache-dir` wins when set, even if it diverges from
        the data-dir tree. Power-user knob preserved."""
        tmp = Path(tempfile.mkdtemp())
        contracts_dir = _make_contracts_dir(tmp)
        custom = tmp / "elsewhere" / "cache"
        captured: dict = {}

        with patch(
            "flowmetrics.materialize.materialize",
            side_effect=_capture(captured),
        ):
            res = CliRunner().invoke(
                cli,
                [
                    "materialize", "demo",
                    "--data-dir", str(tmp / "data"),
                    "--workflows-dir", str(contracts_dir),
                    "--cache-dir", str(custom),
                    "--offline",
                ],
                catch_exceptions=False,
            )
        assert res.exit_code == 0, res.output
        assert captured["cache_dir"] == custom


class TestAllWorkflowsDefault:
    def test_all_omitted_cache_dir_resolves_under_data_dir(self):
        """Same derivation for `--all`: each workflow run gets
        `<data-dir>/.cache/github` when --cache-dir is unset."""
        tmp = Path(tempfile.mkdtemp())
        contracts_dir = _make_contracts_dir(tmp)
        data_dir = tmp / "data"
        seen_cache_dirs: list[Path] = []

        def stub(*, workflow, data_dir, cache_dir, offline):
            seen_cache_dirs.append(cache_dir)
            return RunManifest(
                run_id="test",
                contract_id=workflow.name,
                started_at=_dt.now(UTC),
                completed_at=_dt.now(UTC),
                items_fetched=0,
            )

        with patch(
            "flowmetrics.materialize.materialize",
            side_effect=stub,
        ):
            res = CliRunner().invoke(
                cli,
                [
                    "materialize", "--all",
                    "--data-dir", str(data_dir),
                    "--workflows-dir", str(contracts_dir),
                    "--offline",
                ],
                catch_exceptions=False,
            )
        assert res.exit_code == 0, res.output
        assert seen_cache_dirs, "no workflows ran"
        for cache_dir in seen_cache_dirs:
            assert cache_dir == data_dir / ".cache" / "github"
