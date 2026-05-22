"""E2E: the Period filter bar.

The filter bar is a thin input layer — it emits a `period`
choice (a preset, or Custom = anchor + view_days) and nothing
else; `parse_windows` server-side turns it into the windows
every view reads. Pins:

  - The default Period is "Last 30 days".
  - Picking a preset submits `?period=<name>`.
  - "Custom" reveals Period Ending + View; the date input is
    bounded to the data coverage (no picking a date with no
    data behind it).
  - A custom period drives the chart window.
  - Reset clears the query.
"""

from __future__ import annotations

import contextlib
import re
import socket
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import uvicorn
import yaml
from click.testing import CliRunner
from playwright.sync_api import Page, expect

from flowmetrics.cli import cli

pytestmark = pytest.mark.e2e

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


def _materialise_wide(
    contracts_dir: Path, data_dir: Path, cache_dir: Path
) -> None:
    """Materialise a `wide-demo` contract whose completions span
    ~130 days, plus a few in-flight items.

    The `astral-uv-week` fixture only holds one week of data, so a
    7-day Period and a 90-day Period capture the *same* sample —
    useless for testing that the Period actually drives the
    metrics. `wide-demo` spreads the data wide enough that the
    windows visibly diverge.
    """
    from flowmetrics.compute import WorkItem
    from flowmetrics.contract import Contract
    from flowmetrics.materialise import materialise

    name = "wide-demo"
    (contracts_dir / f"{name}.yaml").write_text(
        yaml.safe_dump({
            "contract": {
                "name": name, "source": "github", "repo": "x/y",
                "start": "2025-01-01", "stop": "2027-12-31",
            }
        })
    )
    now = datetime.now(UTC)

    def _item(item_id: str, *, created, completed):
        return WorkItem(
            item_id=item_id,
            title=f"item {item_id}",
            url=f"https://github.com/x/y/pull/{item_id.lstrip('#')}",
            created_at=created,
            completed_at=completed,
        )

    # One completion every 4 days back ~130 days — 33 items, so a
    # 7-day window catches ~2 and a 90-day window catches ~23.
    completed = [
        _item(
            f"#{i + 1}",
            created=(now - timedelta(days=i * 4 + 2)),
            completed=(now - timedelta(days=i * 4)),
        )
        for i in range(33)
    ]
    in_flight = [
        _item("#901", created=now - timedelta(days=20), completed=None),
        _item("#902", created=now - timedelta(days=8), completed=None),
        _item("#903", created=now - timedelta(days=2), completed=None),
        # A deeply-stale open item — gives the aging chart an
        # outlier so the cap-slider range is meaningful.
        _item("#904", created=now - timedelta(days=400), completed=None),
    ]
    source = MagicMock()
    source.fetch_completed_in_window.return_value = completed
    source.fetch_in_flight.return_value = in_flight
    with patch(
        "flowmetrics.materialise.make_github_source",
        return_value=source,
    ):
        materialise(
            contract=Contract(
                name=name, source="github", repo="x/y",
                start=date(2025, 1, 1), stop=date(2027, 12, 31),
            ),
            data_dir=data_dir,
            cache_dir=cache_dir,
            offline=False,
        )


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

    # A second contract whose data spans ~130 days — the filter-
    # propagation tests need that spread (see `_materialise_wide`).
    _materialise_wide(contracts_dir, data_dir, tmp_path / "cache")

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


class TestPeriodFilterBar:
    def test_default_period_is_last_30_days(
        self, server_url: str, page: Page
    ):
        """No query params → the Period dropdown selects "Last 30
        days" and the Custom fields stay hidden."""
        page.goto(server_url + "/workflows/astral-uv-week")
        page.wait_for_selector("select[name='period']")
        period = page.evaluate(
            "() => document.querySelector(\"select[name='period']\").value"
        )
        assert period == "last-30-days"
        # The Period Ending field is Custom-only.
        expect(page.locator("input[name='anchor']")).to_be_hidden()

    def test_selecting_a_preset_submits_period(
        self, server_url: str, page: Page
    ):
        """Picking a preset auto-submits, emitting just
        `?period=<name>` — no anchor/view_days noise."""
        page.goto(
            server_url + "/workflows/astral-uv-week/metrics/cycle-time"
        )
        page.wait_for_selector("select[name='period']")
        page.select_option("select[name='period']", "last-7-days")
        page.wait_for_url("**period=last-7-days**")
        url = page.evaluate("() => location.href")
        assert "anchor=" not in url, f"preset URL should be clean: {url}"
        assert "view_days=" not in url, f"preset URL should be clean: {url}"

    def test_custom_reveals_period_ending_and_view(
        self, server_url: str, page: Page
    ):
        """Choosing "Custom…" reveals the Period Ending date
        field and the View dropdown without submitting."""
        page.goto(
            server_url + "/workflows/astral-uv-week/metrics/cycle-time"
        )
        page.wait_for_selector("select[name='period']")
        expect(page.locator("input[name='anchor']")).to_be_hidden()
        page.select_option("select[name='period']", "custom")
        expect(page.locator("input[name='anchor']")).to_be_visible()
        expect(page.locator("select[name='view_days']")).to_be_visible()

    def test_cfd_follows_the_period(
        self, server_url: str, page: Page
    ):
        """The Period is a VISUAL window on the CFD — it clamps the
        x-axis (the cumulative math stays full-history). So the CFD
        headline changes with the Period, and the page carries the
        Period bar."""
        def _cfd_headline(period: str) -> str:
            page.goto(
                server_url + "/workflows/astral-uv-week/metrics/cfd"
                f"?period={period}"
            )
            page.wait_for_selector(".metric-strip-headline")
            return page.locator(".metric-strip-headline").inner_text()

        h7 = _cfd_headline("last-7-days")
        h90 = _cfd_headline("last-90-days")
        assert h7 != h90, (
            f"CFD headline should change with the Period; "
            f"7d={h7!r}  90d={h90!r}"
        )
        # The CFD page carries the Period bar (it is period-driven).
        expect(page.locator("select[name='period']")).to_have_count(1)

    def test_period_ending_is_bounded_to_the_data_coverage(
        self, server_url: str, page: Page
    ):
        """The Period Ending date input carries min/max bounding
        it to the dates that actually have data — you can't pick a
        period with no data behind it."""
        page.goto(server_url + "/workflows/astral-uv-week?period=custom")
        page.wait_for_selector("input[name='anchor']")
        lo = page.get_attribute("input[name='anchor']", "min")
        hi = page.get_attribute("input[name='anchor']", "max")
        assert lo and hi, f"date input must be bounded; min={lo} max={hi}"
        # The fixture's data sits inside May 4-10, 2026.
        assert date(2026, 5, 4) <= date.fromisoformat(lo) <= date(2026, 5, 10)
        assert date(2026, 5, 4) <= date.fromisoformat(hi) <= date(2026, 5, 10)

    def test_aging_ignores_the_period_ending(
        self, server_url: str, page: Page
    ):
        """Aging is a "right now" snapshot pinned to the latest
        materialise — a custom Period anchor in the URL must NOT
        move its as-of date, and the page carries no Period bar."""
        page.goto(
            server_url + "/workflows/astral-uv-week/metrics/aging"
            "?period=custom&anchor=2026-05-06&view_days=30"
        )
        page.wait_for_selector(".metric-strip-headline")
        headline = page.locator(".metric-strip-headline").inner_text()
        # The Period anchor must NOT drive aging's as-of date.
        assert "May 06, 2026" not in headline, (
            f"aging must ignore the Period anchor; got {headline!r}"
        )
        # The aging page has no Period filter bar at all.
        expect(page.locator("select[name='period']")).to_have_count(0)

    def test_reset_clears_the_query(self, server_url: str, page: Page):
        """The Reset link drops all filter params."""
        page.goto(
            server_url + "/workflows/astral-uv-week"
            "?period=custom&anchor=2026-05-08&view_days=7"
        )
        page.wait_for_selector("a.filter-reset")
        page.locator("a.filter-reset").click()
        page.wait_for_url("**/workflows/astral-uv-week")
        url = page.evaluate("() => location.href")
        assert "period=" not in url
        assert "anchor=" not in url


class TestFilterPropagationToText:
    """Audit: changing the Period must update every metric's
    dynamic TEXT — headlines and provenance — not only the chart
    SVGs. Uses `wide-demo` (data spans ~130 days) so a 7-day and a
    90-day Period capture visibly different samples."""

    def _strip_headline(
        self, page: Page, server_url: str, metric: str, period: str
    ) -> str:
        page.goto(
            f"{server_url}/workflows/wide-demo/metrics/{metric}"
            f"?period={period}"
        )
        page.wait_for_selector(".metric-strip-headline", timeout=15000)
        return page.locator(".metric-strip-headline").inner_text()

    def test_aging_percentile_provenance_follows_the_period(
        self, server_url: str, page: Page
    ):
        """Aging's percentile sample is the reference window, which
        follows the Period — a 7-day Period draws on fewer
        completed items than a 90-day one, and the headline says
        so. (This is the bug the audit was opened for.)"""
        h7 = self._strip_headline(page, server_url, "aging", "last-7-days")
        h90 = self._strip_headline(
            page, server_url, "aging", "last-90-days"
        )
        assert h7 != h90, (
            f"aging percentile provenance must change with the "
            f"Period; 7d={h7!r}  90d={h90!r}"
        )

    def test_cycle_time_headline_follows_the_period(
        self, server_url: str, page: Page
    ):
        h7 = self._strip_headline(
            page, server_url, "cycle-time", "last-7-days"
        )
        h90 = self._strip_headline(
            page, server_url, "cycle-time", "last-90-days"
        )
        assert h7 != h90, (
            f"cycle-time headline must change with the Period; "
            f"7d={h7!r}  90d={h90!r}"
        )

    def test_throughput_headline_follows_the_period(
        self, server_url: str, page: Page
    ):
        h7 = self._strip_headline(
            page, server_url, "throughput", "last-7-days"
        )
        h90 = self._strip_headline(
            page, server_url, "throughput", "last-90-days"
        )
        assert h7 != h90, (
            f"throughput headline must change with the Period; "
            f"7d={h7!r}  90d={h90!r}"
        )

    def test_forecast_history_window_follows_the_period(
        self, server_url: str, page: Page
    ):
        """The forecast's Monte Carlo sample is the reference
        window — its 'N days of throughput history' must shrink for
        a shorter Period."""
        def _history_days(period: str) -> list[str]:
            page.goto(
                f"{server_url}/workflows/wide-demo/metrics/forecast"
                f"?period={period}"
            )
            page.wait_for_selector("body")
            page.wait_for_timeout(500)
            body = page.locator("body").inner_text()
            return re.findall(
                r"over ([\d,]+) days of throughput history", body
            )

        d7 = _history_days("last-7-days")
        d90 = _history_days("last-90-days")
        assert d7, "forecast must report its throughput-history span"
        assert d7 != d90, (
            f"forecast history span must change with the Period; "
            f"7d={d7}  90d={d90}"
        )


class TestFilterPropagationThroughInteractions:
    """Audit: an HTMX interaction that re-fetches a fragment
    (a forecast slider, a chart drill-down) must carry the Period
    — otherwise the fragment silently reverts to the default
    window mid-interaction."""

    def test_forecast_slider_keeps_the_period(
        self, server_url: str, page: Page
    ):
        """Dragging a forecast slider re-fetches the panel — that
        fetch must carry the Period, or the forecast's history
        window silently snaps back to the default."""
        page.goto(
            f"{server_url}/workflows/wide-demo/metrics/forecast"
            "?period=last-7-days"
        )
        page.wait_for_selector("#items-slider", timeout=15000)
        page.wait_for_timeout(700)

        def _history() -> list[str]:
            body = page.locator("body").inner_text()
            return re.findall(
                r"over ([\d,]+) days of throughput history", body
            )

        before = _history()
        # Drag the items slider — set value + fire the events HTMX
        # listens for (`fill` doesn't drive range inputs reliably).
        page.evaluate(
            """() => {
                const s = document.getElementById('items-slider');
                s.value = String(Math.min(200, Number(s.value) + 60));
                s.dispatchEvent(new Event('input', {bubbles: true}));
                s.dispatchEvent(new Event('change', {bubbles: true}));
            }"""
        )
        page.wait_for_timeout(1200)  # hx delay 100ms + re-render
        after = _history()
        assert before and after, (
            f"forecast must report its history span; "
            f"before={before} after={after}"
        )
        assert before == after, (
            f"a forecast slider drag must not change the history "
            f"window; before={before} after={after}"
        )

    def test_throughput_bar_click_drilldown_keeps_the_period(
        self, server_url: str, page: Page
    ):
        """Clicking a throughput bar drills into the work-items
        table — that request must carry the Period so the
        drill-down keeps the selected view window."""
        page.goto(
            f"{server_url}/workflows/wide-demo/metrics/throughput"
            "?period=last-90-days"
        )
        page.wait_for_selector("#throughput-chart svg", timeout=15000)
        page.wait_for_selector("#work-items", timeout=10000)
        page.wait_for_timeout(800)
        with page.expect_request(
            "**/api/internal/work-items**"
        ) as info:
            clicked = page.evaluate(
                """() => {
                    const groups = document.querySelectorAll(
                        '#throughput-chart svg g.mark-rect');
                    if (!groups.length) return false;
                    const bars = groups[groups.length - 1]
                        .querySelectorAll('path');
                    let tallest = null, h = 0;
                    for (const p of bars) {
                        const r = p.getBoundingClientRect();
                        if (r.height > h) { h = r.height; tallest = p; }
                    }
                    if (!tallest) return false;
                    const r = tallest.getBoundingClientRect();
                    tallest.dispatchEvent(new MouseEvent('click', {
                        bubbles: true, cancelable: true,
                        clientX: r.x + r.width / 2,
                        clientY: r.y + r.height / 2,
                        view: window}));
                    return true;
                }"""
            )
            assert clicked, "could not find a throughput bar to click"
        assert "period=last-90-days" in info.value.url, (
            f"throughput drill-down must carry the Period; "
            f"url={info.value.url}"
        )
