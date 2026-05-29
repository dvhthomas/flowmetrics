"""End-to-end (offline): a typed-matcher contract materialises so the
warehouse transitions carry the user's STEP names, not the adapter's raw
stages (#2 slice E).

Runs the real `flow materialise` CLI against the pinned astral-sh/uv
fixture cache (2026-05-04..2026-05-10) — no network. Proves the
remap-at-materialise path end to end, and that the WIP step name lines
up with the warehouse stages (so the dashboard's WIP filter will work).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.contract import parse_contract_text

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"

_YAML = """contract:
  name: uv-typed
  source: github
  repo: astral-sh/uv
  start: '2026-05-04'
  stop: '2026-05-10'
  steps:
    - name: Open
      wip: false
      matches:
        - event: pr-opened
    - name: In Review
      wip: true
      matches:
        - event: pr-ready
        - event: changes-requested
        - event: approved
        - label: Awaiting Review
    - name: Done
      wip: false
      matches:
        - event: pr-merged
"""


def _materialise(tmp_path: Path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    data = tmp_path / "data"
    (contracts / "uv-typed.yaml").write_text(_YAML)
    res = CliRunner().invoke(
        cli,
        [
            "materialise", "uv-typed",
            "--data-dir", str(data),
            "--workflows-dir", str(contracts),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output
    return data, contracts


def test_transitions_carry_step_names_not_adapter_stages(tmp_path):
    data, _ = _materialise(tmp_path)
    glob = str(
        data / "transitions" / "contract_id=uv-typed" / "**" / "*.parquet"
    )
    stages = {
        r[0]
        for r in duckdb.sql(
            f"SELECT DISTINCT stage FROM read_parquet('{glob}')"
        ).fetchall()
    }
    # Every stage is one of the user's step names (or _unmatched) —
    # the raw adapter vocabulary (Merged / Awaiting Review / Approved …)
    # must not leak through.
    assert stages <= {"Open", "In Review", "Done", "_unmatched"}, stages
    assert "Done" in stages, f"merged PRs should reach Done; got {stages}"
    # The repo marks review via a label ("Awaiting Review"); the label
    # matcher catches it, so In Review is populated and nothing is left
    # unmatched.
    assert "In Review" in stages, f"label matcher should fill In Review; {stages}"
    assert "_unmatched" not in stages, f"all transitions mapped; got {stages}"
    assert "Merged" not in stages and "Awaiting Review" not in stages


def test_wip_step_name_aligns_with_warehouse_stages(tmp_path):
    _, contracts = _materialise(tmp_path)
    c = parse_contract_text((contracts / "uv-typed.yaml").read_text(), "uv-typed")
    # The WIP filter keys off states.wip (step names); after remap the
    # warehouse stages ARE these names, so the filter resolves.
    assert c.states is not None
    assert c.states.wip == ("In Review",)
