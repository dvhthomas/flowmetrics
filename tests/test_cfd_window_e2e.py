"""E2E: CFD chart x-axis domain matches the spec window.

The bug this guards against: with no time window on the render
call, the CFD reaches back across the entire transition history
of every in-flight item (multi-year for active OSS repos). The
x-axis becomes an unreadable smear of ~700 daily tick labels and
the metric is "scoped" by nothing.

Acceptance:
  - When a contract sets `start`/`stop`, the chart shows EXACTLY
    that window — every visible date label falls within
    [start, stop].
  - When neither bound is set, the chart caps at the default 90
    days back from the data's most recent date.

Why e2e: per `feedback_test_credibility_rule`, UI-shape claims
need browser evidence. The unit tests pin the render payload's
`daily` list, but the rendered SVG axis is what the user sees.
Drive Playwright against a real uvicorn process and inspect SVG
<text> nodes.

Default pytest run skips e2e. Run: `uv run pytest -m e2e
tests/test_cfd_window_e2e.py`.
"""

from __future__ import annotations

import contextlib
import re
import socket
import threading
import time
from datetime import date
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
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    """Materialise the fixture against a 7-day contract window and
    serve via uvicorn. The contract has `start: 2026-05-04` and
    `stop: 2026-05-10` so the CFD must show exactly that range.
    """
    from flowmetrics.app import create_app

    tmp_path = tmp_path_factory.mktemp("cfd-window-e2e")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp_path / "data"
    name = "astral-uv-week"

    contract_yaml = {
        "contract": {
            "name": name,
            "source": "github",
            "repo": "astral-sh/uv",
            "start": "2026-05-04",
            "stop": "2026-05-10",
        }
    }
    (contracts_dir / f"{name}.yaml").write_text(yaml.safe_dump(contract_yaml))
    res = CliRunner().invoke(
        cli,
        [
            "materialise",
            name,
            "--data-dir",
            str(data_dir),
            "--contracts-dir",
            str(contracts_dir),
            "--cache-dir",
            str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, f"fixture materialise failed: {res.output}"

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


# Vega-Lite renders nominal x-axis labels as `<text>` nodes inside
# the chart SVG. Pull them all out and assert each parses to a
# date in [start, stop].
_DATE_TEXT_RE = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}$")
# Headline pattern: "N days (May 04, 2026 – May 10, 2026)"
_HEADLINE_RANGE_RE = re.compile(
    r"\((?P<from>[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})"
    r"\s+[–—-]\s+"
    r"(?P<to>[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\)"
)


class TestCfdVisualPeriod:
    """The Period is a VISUAL window on the CFD — it clamps the
    x-axis viewport. The cumulative math stays full-history (the
    carry-in at the window's left edge is the true running total);
    the Period just chooses which dates are shown."""

    def test_detail_page_renders_chart_svg(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cfd")
        page.wait_for_selector("#cfd-chart svg", timeout=15000)
        expect(page.locator("#cfd-chart svg")).to_be_visible()

    def test_cfd_page_has_the_period_bar(
        self, server_url: str, page: Page
    ):
        """The CFD is period-driven (the Period is its visual
        window), so its page carries the Period filter bar."""
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cfd")
        page.wait_for_selector("#cfd-chart svg", timeout=15000)
        expect(page.locator("select[name='period']")).to_have_count(1)

    def test_axis_clamps_to_the_period_window(
        self, server_url: str, page: Page
    ):
        """A 7-day Period clamps the CFD x-axis to that 7-day
        window — every visible date tick falls inside it."""
        page.goto(
            server_url + "/workflows/astral-uv-week/metrics/cfd"
            "?period=custom&anchor=2026-05-10&view_days=7"
        )
        page.wait_for_selector("#cfd-chart svg", timeout=15000)
        page.wait_for_timeout(500)
        svg_texts: list[str] = page.evaluate(
            """() => Array.from(
                document.querySelectorAll('#cfd-chart svg text')
            ).map(t => t.textContent.trim())"""
        )
        date_labels = [s for s in svg_texts if _DATE_TEXT_RE.match(s)]
        assert date_labels, (
            f"expected date tick labels on the CFD x-axis; "
            f"SVG <text> nodes were: {svg_texts}"
        )
        from datetime import datetime
        start, stop = date(2026, 5, 4), date(2026, 5, 10)
        for label in date_labels:
            parsed = datetime.strptime(f"{label} 2026", "%b %d %Y").date()
            assert start <= parsed <= stop, (
                f"x-axis tick {label!r} ({parsed}) is outside the "
                f"7-day Period window [{start} – {stop}]"
            )
