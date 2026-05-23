"""`flow serve` discoverability — the operator must always be
able to tell WHICH directories the running server is scanning.

The defaults (`./data`, `./contracts`) routinely mismatch what
the operator expects: they run `flow serve` from one place and
the demo data lives somewhere else. Without the resolved paths
surfaced, the page is silently empty and the error is "huh?".

This file pins two contracts:

  - The startup banner names the resolved data_dir +
    contracts_dir alongside the bind URL.
  - The home page's empty-workflows state names the absolute
    contracts_dir path the server is scanning.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient


@pytest.fixture
def empty_dirs(tmp_path: Path) -> tuple[Path, Path]:
    data = tmp_path / "data"
    contracts = tmp_path / "contracts"
    data.mkdir()
    contracts.mkdir()
    return data, contracts


class TestServeStartupBanner:
    def test_banner_includes_both_resolved_paths(self, empty_dirs, monkeypatch):
        from flowmetrics.cli import cli

        data, contracts = empty_dirs

        # Skip uvicorn's blocking run — we only need the banner.
        import flowmetrics.cli as cli_mod
        called = {}

        def _fake_run(app, *, host, port, log_level):
            called["yes"] = True

        monkeypatch.setattr(cli_mod, "_assert_port_available", lambda *a, **kw: None)
        import uvicorn
        monkeypatch.setattr(uvicorn, "run", _fake_run)

        result = CliRunner().invoke(
            cli, [
                "serve",
                "--port", "0",
                "--data-dir", str(data),
                "--workflows-dir", str(contracts),
            ],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert called.get("yes")
        # The banner names BOTH directories so a confused operator
        # immediately sees what's being scanned.
        assert str(data.resolve()) in result.output
        assert str(contracts.resolve()) in result.output


class TestHomeEmptyStateNamesPath:
    def test_empty_workflows_page_shows_the_contracts_dir_path(
        self, empty_dirs,
    ):
        from flowmetrics.app import create_app

        data, contracts = empty_dirs
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.get("/")
        assert r.status_code == 200
        # Empty-state copy is present.
        assert "No workflows yet" in r.text
        # The resolved absolute contracts_dir path is in the page —
        # operator can see EXACTLY which directory was scanned.
        assert str(contracts.resolve()) in r.text
