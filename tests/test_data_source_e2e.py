"""E2E: the Data Source page — coverage view + browser-driven
backfill (detached subprocess + JSON status file + HTMX poll).

The app is created with `offline=True` so the backfill subprocess
materialises from the fixture cache and never touches the network.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import yaml
from click.testing import CliRunner
from playwright.sync_api import Page, expect

from flowmetrics.cli import cli

pytestmark = pytest.mark.e2e

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port,
            log_level="error", access_log=False,
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    from flowmetrics.app import create_app

    tmp = tmp_path_factory.mktemp("data-source-e2e")
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"
    name = "astral-uv-week"
    (contracts_dir / f"{name}.yaml").write_text(
        yaml.safe_dump({
            "contract": {
                "name": name, "source": "github",
                "repo": "astral-sh/uv",
                "start": "2026-05-04", "stop": "2026-05-10",
            }
        })
    )
    res = CliRunner().invoke(
        cli,
        [
            "materialise", name,
            "--data-dir", str(data_dir),
            "--workflows-dir", str(contracts_dir),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output

    # `offline=True` → the backfill subprocess materialises from
    # the fixture cache; no test ever touches the network.
    app = create_app(
        data_dir=data_dir,
        contracts_dir=contracts_dir,
        cache_dir=FIXTURE_CACHE,
        offline=True,
    )
    port = _free_port()
    thread = _ServerThread(app, port)
    thread.start()
    for _ in range(50):
        with (
            contextlib.suppress(OSError),
            socket.create_connection(("127.0.0.1", port), timeout=0.2),
        ):
            break
        time.sleep(0.1)
    else:
        thread.stop()
        raise RuntimeError("uvicorn did not start in time")
    yield f"http://127.0.0.1:{port}"
    thread.stop()
    thread.join(timeout=3)


class TestDataSourcePage:
    def test_page_renders_coverage_and_backfill_controls(
        self, server_url: str, page: Page
    ):
        """The page shows the coverage chart and the backfill
        form."""
        page.goto(
            server_url + "/workflows/astral-uv-week/data-source"
        )
        page.wait_for_selector(".ds-backfill-form")
        expect(page.locator("#ds-coverage")).to_be_visible()
        expect(
            page.locator(".ds-backfill-form button[type=submit]")
        ).to_be_visible()
        # The fixture has completions, so the coverage chart draws.
        page.wait_for_selector("#ds-coverage svg", timeout=15000)

    def test_backfill_round_trip_runs_to_completion(
        self, server_url: str, page: Page
    ):
        """Submitting a backfill spawns the subprocess; the
        progress fragment polls the JSON status file and reaches
        a 'complete' state, then auto-refreshes the page so the
        coverage chart picks up the new data. The window + anchor
        controls resolve to the fixture's range (May 4–10, 2026)."""
        page.goto(
            server_url + "/workflows/astral-uv-week/data-source"
        )
        page.wait_for_selector(".ds-backfill-form")
        # 7-day window up to 2026-05-10 → since 2026-05-04.
        page.select_option("#ds-days", "7")
        page.fill("#ds-anchor", "2026-05-10")
        # The resolved range is shown read-only.
        expect(page.locator("#ds-range")).to_contain_text("2026-05-04")
        page.click(".ds-backfill-form button[type=submit]")
        # The progress fragment appears, then polls to done.
        page.wait_for_selector(
            "#backfill-progress-row.backfill-progress--done",
            timeout=45000,
        )
        text = page.locator("#backfill-progress-row").inner_text()
        assert "complete" in text.lower(), (
            f"backfill should report completion; got {text!r}"
        )
        # The done fragment auto-refreshes the whole page (1.5s
        # delay) so the coverage chart re-renders against the
        # freshly-backfilled warehouse.
        page.wait_for_timeout(2500)
        page.wait_for_selector("#ds-coverage svg", timeout=15000)
        expect(
            page.locator("#backfill-progress-row.backfill-progress--done")
        ).to_be_visible()
