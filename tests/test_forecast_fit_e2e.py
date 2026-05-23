"""E2E: Forecast charts fit their container on FIRST PAINT.

Bug this guards: on initial page load the "When will it be done?"
chart (nominal x, sparse bars when items=20) renders WIDER than
its container and overflows into the neighbouring panel. Dragging
the slider triggers an HTMX swap that re-embeds the chart in a
fully-settled layout, and the bug disappears. Cause: with
`width: "container"` + nominal x, Vega-Lite reads container
width at embed time; when CSS grid layout hasn't finished
computing column widths on first paint, the read returns a wider
value than the final column.

The fix is a post-embed `view.resize()` via `requestAnimationFrame`
so the view re-measures after layout settles. This test pins
that the SVG fits the container on FIRST PAGE LOAD, before any
slider interaction.
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
from playwright.sync_api import Page

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
    from flowmetrics.app import create_app

    tmp_path = tmp_path_factory.mktemp("forecast-fit-e2e")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp_path / "data"
    name = "astral-uv-week"
    (contracts_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
                    "name": name,
                    "source": "github",
                    "repo": "astral-sh/uv",
                    "start": "2026-05-04",
                    "stop": "2026-05-10",
                }
            }
        )
    )
    res = CliRunner().invoke(
        cli,
        [
            "materialise",
            name,
            "--data-dir",
            str(data_dir),
            "--workflows-dir",
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


class TestForecastChartsFitContainer:
    """Both forecast charts (When-Done, How-Many) must fit inside
    their `.forecast-panel` parents on first paint — no overflow
    into the neighbouring grid cell."""

    def test_when_done_chart_fits_panel_on_first_load(
        self, server_url: str, page: Page
    ):
        """The nominal-x When-Done chart is the one that breaks:
        sparse bars + nominal scale + width:container = Vega
        falls back to step-based sizing when the container's
        width isn't stable at embed time. Assert chart SVG width
        ≤ container width on initial page load."""
        # Use a wide viewport so the grid is 2-col (matches user's
        # bug screenshot); narrow viewports collapse the grid and
        # the bug doesn't reproduce.
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(server_url + "/workflows/astral-uv-week/metrics/forecast")
        page.wait_for_selector("#forecast-when-done-chart svg", timeout=15000)
        # Let RAF + ResizeObserver settle so a CORRECTLY-sized
        # chart has time to land. Layout-thrash bugs only show up
        # if the chart is STILL overflowing after settle.
        page.wait_for_timeout(500)

        sizes = page.evaluate(
            """() => {
                const chart = document.querySelector('#forecast-when-done-chart');
                const svg = chart && chart.querySelector('svg');
                const panel = chart && chart.closest('.forecast-panel');
                if (!chart || !svg || !panel) return null;
                return {
                    chartWidth: chart.getBoundingClientRect().width,
                    svgWidth: svg.getBoundingClientRect().width,
                    panelWidth: panel.getBoundingClientRect().width,
                };
            }"""
        )
        assert sizes is not None, (
            "could not locate forecast-when-done-chart / svg / panel"
        )
        # The SVG must not overflow its container. Allow ~2px for
        # subpixel rendering rounding.
        assert sizes["svgWidth"] <= sizes["chartWidth"] + 2, (
            f"When-Done SVG ({sizes['svgWidth']}px) overflows its chart "
            f"container ({sizes['chartWidth']}px) on first paint. "
            f"Panel is {sizes['panelWidth']}px. Bug: nominal x-axis "
            f"with sparse bars + width:container falls back to step-"
            f"sizing when container's width isn't measurable at embed "
            f"time; a post-embed view.resize() in requestAnimationFrame "
            f"would force a re-measure after layout settles."
        )

    def test_how_many_chart_fits_panel_on_first_load(
        self, server_url: str, page: Page
    ):
        """Mirror assertion on the quantitative-x How-Many chart.
        It doesn't break in the wild (quantitative axes don't have
        the step fallback), but the test exists to catch
        regressions in either direction."""
        page.set_viewport_size({"width": 1440, "height": 900})
        page.goto(server_url + "/workflows/astral-uv-week/metrics/forecast")
        page.wait_for_selector("#forecast-how-many-chart svg", timeout=15000)
        page.wait_for_timeout(500)
        sizes = page.evaluate(
            """() => {
                const chart = document.querySelector('#forecast-how-many-chart');
                const svg = chart && chart.querySelector('svg');
                if (!chart || !svg) return null;
                return {
                    chartWidth: chart.getBoundingClientRect().width,
                    svgWidth: svg.getBoundingClientRect().width,
                };
            }"""
        )
        assert sizes is not None, (
            "could not locate forecast-how-many-chart / svg"
        )
        assert sizes["svgWidth"] <= sizes["chartWidth"] + 2, (
            f"How-Many SVG ({sizes['svgWidth']}px) overflows its chart "
            f"container ({sizes['chartWidth']}px) on first paint."
        )
