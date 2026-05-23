"""`flow materialise --since/--until` for targeted backfills.

The contract YAML defines a default fetch window (start / stop).
Operators sometimes need to backfill a specific range without
editing the YAML — e.g., the aging page's empty-state message
suggests `flow materialise <name> --since X --until Y` to fill a
coverage gap. These tests pin the CLI surface and the override
semantics:

  - `--since` and `--until` are optional. Omitted → contract
    window applies (existing behavior).
  - Either may be set independently. Setting just one overrides
    that endpoint; the other keeps the contract default.
  - Both expect ISO `YYYY-MM-DD` (UTC). Invalid → exit non-zero
    with a clear message.
  - When set, the materialise step calls
    `source.fetch_completed_in_window(since, until)` with the
    overrides, NOT the contract's start/stop.

The override doesn't mutate the YAML; it's a per-invocation
window applied to the same Contract config.
"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli


FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


def _make_contracts_dir(tmp: Path) -> Path:
    """A workflow YAML with start=May 04, stop=May 10."""
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    (contracts_dir / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
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
    """Replacement for `materialise.materialise` that records the
    Contract it was called with and returns a stub manifest. The
    CLI's only side effect we care about for these tests is the
    contract it produced — the fetch + Parquet write is the
    materialise unit's responsibility, separately tested."""

    from datetime import UTC, datetime as _dt

    from flowmetrics.materialise import RunManifest

    def stub(*, contract, data_dir, cache_dir, offline):
        captured["contract"] = contract
        return RunManifest(
            run_id="test",
            contract_id=contract.name,
            started_at=_dt.now(UTC),
            completed_at=_dt.now(UTC),
            items_fetched=0,
        )

    return stub


class TestSinceUntilFlags:
    def test_invoking_without_overrides_uses_contract_window(self):
        tmp = Path(tempfile.mkdtemp())
        contracts_dir = _make_contracts_dir(tmp)
        captured: dict = {}

        with patch(
            "flowmetrics.cli.run_materialise" if False else
            "flowmetrics.materialise.materialise",
            side_effect=_capture(captured),
        ):
            res = CliRunner().invoke(
                cli,
                [
                    "materialise", "demo",
                    "--data-dir", str(tmp / "data"),
                    "--workflows-dir", str(contracts_dir),
                    "--cache-dir", str(FIXTURE_CACHE),
                    "--offline",
                ],
                catch_exceptions=False,
            )
        assert res.exit_code == 0, res.output
        c = captured["contract"]
        assert c.start == date(2026, 5, 4)
        assert c.stop == date(2026, 5, 10)

    def test_since_overrides_window_start(self):
        tmp = Path(tempfile.mkdtemp())
        contracts_dir = _make_contracts_dir(tmp)
        captured: dict = {}

        with patch(
            "flowmetrics.materialise.materialise",
            side_effect=_capture(captured),
        ):
            res = CliRunner().invoke(
                cli,
                [
                    "materialise", "demo",
                    "--since", "2026-05-06",
                    "--data-dir", str(tmp / "data"),
                    "--workflows-dir", str(contracts_dir),
                    "--cache-dir", str(FIXTURE_CACHE),
                    "--offline",
                ],
                catch_exceptions=False,
            )
        assert res.exit_code == 0, res.output
        c = captured["contract"]
        assert c.start == date(2026, 5, 6), (
            f"--since should override start; got {c.start}"
        )
        assert c.stop == date(2026, 5, 10)

    def test_until_overrides_window_stop(self):
        tmp = Path(tempfile.mkdtemp())
        contracts_dir = _make_contracts_dir(tmp)
        captured: dict = {}

        with patch(
            "flowmetrics.materialise.materialise",
            side_effect=_capture(captured),
        ):
            res = CliRunner().invoke(
                cli,
                [
                    "materialise", "demo",
                    "--until", "2026-05-08",
                    "--data-dir", str(tmp / "data"),
                    "--workflows-dir", str(contracts_dir),
                    "--cache-dir", str(FIXTURE_CACHE),
                    "--offline",
                ],
                catch_exceptions=False,
            )
        assert res.exit_code == 0, res.output
        c = captured["contract"]
        assert c.start == date(2026, 5, 4)
        assert c.stop == date(2026, 5, 8)

    def test_both_since_and_until_can_be_set_together(self):
        tmp = Path(tempfile.mkdtemp())
        contracts_dir = _make_contracts_dir(tmp)
        captured: dict = {}

        with patch(
            "flowmetrics.materialise.materialise",
            side_effect=_capture(captured),
        ):
            res = CliRunner().invoke(
                cli,
                [
                    "materialise", "demo",
                    "--since", "2026-05-06",
                    "--until", "2026-05-08",
                    "--data-dir", str(tmp / "data"),
                    "--workflows-dir", str(contracts_dir),
                    "--cache-dir", str(FIXTURE_CACHE),
                    "--offline",
                ],
                catch_exceptions=False,
            )
        assert res.exit_code == 0, res.output
        c = captured["contract"]
        assert c.start == date(2026, 5, 6)
        assert c.stop == date(2026, 5, 8)

    def test_malformed_since_exits_nonzero_with_clear_message(self):
        """`--since not-a-date` → exit non-zero. Click parses the
        value as a Click `DateTime`, so the error comes from Click;
        we just confirm the operator sees a useful message."""
        tmp = Path(tempfile.mkdtemp())
        contracts_dir = _make_contracts_dir(tmp)
        res = CliRunner().invoke(
            cli,
            [
                "materialise",
                "demo",
                "--since",
                "not-a-date",
                "--data-dir",
                str(tmp / "data"),
                "--workflows-dir",
                str(contracts_dir),
                "--cache-dir",
                str(FIXTURE_CACHE),
                "--offline",
            ],
            catch_exceptions=False,
        )
        assert res.exit_code != 0
        # Click's default error mentions the offending flag.
        assert "since" in res.output.lower() or "date" in res.output.lower()
