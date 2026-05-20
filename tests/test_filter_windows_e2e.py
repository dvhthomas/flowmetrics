"""E2E: filter-bar view + reference windows propagate through
the URL to every chart that consumes them.

Pins:
  - Defaults (no query params): 30-day view, 14-day reference
    both anchored to today UTC. Pre-filled in the filter-bar
    inputs.
  - URL overrides (?view_from=...&view_to=...&ref_from=...&ref_to=...)
    re-render the charts with the new windows and keep the
    inputs in sync.
  - Reset link clears all window params from the URL.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from datetime import UTC, date, datetime
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

    tmp_path = tmp_path_factory.mktemp("filter-windows-e2e")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp_path / "data"
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
            "--contracts-dir", str(contracts_dir),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output

    app = create_app(data_dir=data_dir, contracts_dir=contracts_dir)
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


class TestFilterBarWindows:
    def test_inputs_default_to_30_day_view_and_14_day_reference(
        self, server_url: str, page: Page
    ):
        """No query params → both windows populate with defaults:
        30-day view, 14-day reference. Anchored to the latest
        completion date in the warehouse (so defaults always
        produce non-empty charts), with fallback to today UTC
        for empty warehouses."""
        from datetime import date as _date, timedelta
        page.goto(server_url + "/workflows/astral-uv-week")
        page.wait_for_selector("input[name='view_from']")

        def _val(name: str) -> str:
            return page.evaluate(
                f"() => document.querySelector(\"input[name='{name}']\").value"
            )
        view_from = _date.fromisoformat(_val("view_from"))
        view_to = _date.fromisoformat(_val("view_to"))
        ref_from = _date.fromisoformat(_val("ref_from"))
        ref_to = _date.fromisoformat(_val("ref_to"))

        # View window: 30 days inclusive.
        assert (view_to - view_from).days == 29
        # Reference period: 14 days inclusive.
        assert (ref_to - ref_from).days == 13
        # Both windows end on the SAME anchor (data-max date).
        assert view_to == ref_to
        # Anchor should be inside the fixture's window (May 4-10, 2026).
        assert _date(2026, 5, 4) <= view_to <= _date(2026, 5, 10), (
            f"default anchor should be the data-max completion date "
            f"(within May 4-10, 2026 for the fixture); got {view_to}"
        )

    def test_url_params_override_defaults_in_inputs(
        self, server_url: str, page: Page
    ):
        """URL params land in the input values verbatim."""
        page.goto(
            server_url + "/workflows/astral-uv-week"
            "?view_from=2026-05-04&view_to=2026-05-10"
            "&ref_from=2026-05-04&ref_to=2026-05-07"
        )
        page.wait_for_selector("input[name='view_from']")

        def _val(name: str) -> str:
            return page.evaluate(
                f"() => document.querySelector(\"input[name='{name}']\").value"
            )
        assert _val("view_from") == "2026-05-04"
        assert _val("view_to") == "2026-05-10"
        assert _val("ref_from") == "2026-05-04"
        assert _val("ref_to") == "2026-05-07"

    def test_view_window_propagates_to_cfd_axis(
        self, server_url: str, page: Page
    ):
        """View window's `to` shows up in the CFD headline (the
        '… days (DATE – DATE)' range). Pins that the URL → render
        → chart pipeline actually uses the supplied window."""
        page.goto(
            server_url + "/workflows/astral-uv-week/metrics/cfd"
            "?view_from=2026-05-04&view_to=2026-05-10"
        )
        page.wait_for_selector("#cfd-chart svg", timeout=15000)
        body_text = page.locator("body").inner_text()
        assert "7 days" in body_text, (
            "CFD headline should report a 7-day window (May 4 – May 10 "
            f"inclusive); page text excerpt: {body_text[:1500]}"
        )
        assert "May 4, 2026" in body_text or "May 04, 2026" in body_text
        assert "May 10, 2026" in body_text

    def test_apply_button_submits_form_to_same_url(
        self, server_url: str, page: Page
    ):
        """Setting an input and clicking 'Apply' navigates to the
        same path with the new query params."""
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cfd")
        page.wait_for_selector("input[name='view_from']")
        page.fill("input[name='view_from']", "2026-05-04")
        page.fill("input[name='view_to']", "2026-05-10")
        page.fill("input[name='ref_from']", "2026-05-04")
        page.fill("input[name='ref_to']", "2026-05-07")
        page.locator("button.filter-apply").click()
        page.wait_for_url("**view_from=2026-05-04**view_to=2026-05-10**")
        # And the CFD now reflects the window.
        page.wait_for_selector("#cfd-chart svg", timeout=15000)
        assert "7 days" in page.locator("body").inner_text()

    def test_reset_link_clears_query_params(
        self, server_url: str, page: Page
    ):
        """The 'Reset to defaults' link drops all query params."""
        page.goto(
            server_url + "/workflows/astral-uv-week"
            "?view_from=2026-05-04&view_to=2026-05-10"
        )
        page.wait_for_selector("a.filter-reset")
        page.locator("a.filter-reset").click()
        page.wait_for_url("**/workflows/astral-uv-week")
        # No query string in the URL.
        url = page.evaluate("() => location.href")
        assert "view_from" not in url
        assert "ref_from" not in url
