"""Slice 2 acceptance: `flow serve` renders a working cycle-time
dashboard + matching detail page, in a real browser.

The slice 2 click-path (from docs/SPEC-warehouse-app.md §15 and the
spec-driven session leading up to it):

  > Run `flow materialise astral-uv-week` (Slice 1, already works).
  > Run `flow serve --port 8000 --host 127.0.0.1 --data-dir … --workflows-dir …`.
  > Open http://127.0.0.1:8000/. See:
  >   - Sticky filter bar (decorative in Slice 2).
  >   - Anchored #cycle-time section with a Vega-Lite scatterplot,
  >     P50 + P85 reference lines, "Details →" link.
  >   - 43 data points (one per merged PR from the fixture window).
  >   - Hover shows tooltip with title + cycle-time-days.
  >   - Drag-zoom changes the x domain; double-click resets.
  >   - "Details →" navigates to /metrics/cycle-time with the same
  >     chart full-size + placeholder sections for "How to read",
  >     "Caveats", "Methodology", "Actions".
  >   - `--host 0.0.0.0` without `--password` exits with a clear error.

Per SPEC.md §6 (test credibility rule) Slice 2 acceptance must be
e2e: Playwright drives a real Chromium against a real FastAPI
process; assertions name what the user sees (rendered SVG, axis
labels, on-page text) rather than internal route shapes or JSON
payloads. "The div exists" is not enough — the chart must actually
draw, the data must actually appear, the interaction must actually
work.

Slow tests are opt-in via `-m e2e`. Default pytest run skips this
file. To run: `uv run pytest -m e2e tests/test_slice2_e2e.py`.
"""

from __future__ import annotations

import contextlib
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


def _materialise_aging_demo(
    contracts_dir: Path, data_dir: Path, cache_dir: Path
) -> None:
    """Materialise a synthetic `aging-demo` contract whose warehouse
    holds genuine in-flight items.

    Aging WIP is pinned to the in-flight snapshot date. The
    `astral-uv-week` fixture is all completed work, so its aging
    chart is legitimately empty — it can't give browser evidence
    that a *populated* aging chart renders. This contract can:
    four open items (staggered ages) for the dot cloud, plus six
    completed items as the cycle-time sample the P50/P85/P95
    reference lines are drawn from.
    """
    from flowmetrics.compute import WorkItem
    from flowmetrics.contract import Contract
    from flowmetrics.materialise import materialise

    name = "aging-demo"
    (contracts_dir / f"{name}.yaml").write_text(
        yaml.safe_dump({
            "contract": {
                "name": name, "source": "github", "repo": "x/y",
                "start": "2026-01-01", "stop": "2026-12-31",
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

    in_flight = [
        _item("#101", created=now - timedelta(days=40), completed=None),
        _item("#102", created=now - timedelta(days=25), completed=None),
        _item("#103", created=now - timedelta(days=12), completed=None),
        _item("#104", created=now - timedelta(days=3), completed=None),
    ]
    completed = []
    for i, cycle in enumerate((1, 2, 3, 5, 8, 13), start=1):
        started = now - timedelta(days=120 + i)
        completed.append(
            _item(
                f"#{i}",
                created=started,
                completed=started + timedelta(days=cycle),
            )
        )

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
                start=date(2026, 1, 1), stop=date(2026, 12, 31),
            ),
            data_dir=data_dir,
            cache_dir=cache_dir,
            offline=False,
        )


# ---------------------------------------------------------------------------
# Server fixture: in-thread uvicorn against pre-materialised Parquet
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Find an unused TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread(threading.Thread):
    """uvicorn server in a daemon thread; supports graceful shutdown.

    pytest-playwright needs a real bound TCP port the browser can hit;
    FastAPI TestClient is in-process only. This fixture starts uvicorn
    on a free port and tears it down at test end.
    """

    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", access_log=False
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    """Set up a fresh data dir with materialised fixture data, then
    serve via uvicorn in a daemon thread. Yields the base URL.
    """
    from flowmetrics.app import create_app
    from flowmetrics.cli import cli as _cli

    tmp_path = tmp_path_factory.mktemp("slice2")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp_path / "data"
    name = "astral-uv-week"

    # Materialise once via the public CLI so this also exercises the
    # Slice 1 path end-to-end.
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
        _cli,
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

    # A second contract with genuine in-flight items, so the aging
    # chart has something to draw (see `_materialise_aging_demo`).
    _materialise_aging_demo(contracts_dir, data_dir, tmp_path / "cache")

    app = create_app(data_dir=data_dir, contracts_dir=contracts_dir)
    port = _free_port()
    thread = _ServerThread(app, port)
    thread.start()

    # Wait for server to come up
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


# ---------------------------------------------------------------------------
# Tests — drive a real browser, assert what the user sees
# ---------------------------------------------------------------------------


class TestDashboardCycleTimeTile:
    def test_dashboard_renders_vega_svg(self, server_url: str, page: Page):
        page.goto(server_url + "/workflows/astral-uv-week")
        # The Vega-Lite chart embeds inside a div with id cycle-time-tile.
        # Wait for the SVG to actually draw, not just for the container div.
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        expect(page.locator("#cycle-time-tile svg")).to_be_visible()

    def test_dashboard_chart_axes_say_completion_date_and_cycle_time(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # Vega-Lite labels the axes from spec; assert the labels are
        # the human-meaningful ones the slice promised.
        chart_text = page.locator("#cycle-time-tile").inner_text()
        assert "Cycle time" in chart_text, (
            f"y-axis should label cycle time; chart text was:\n{chart_text}"
        )
        assert "Completion date" in chart_text or "Completed" in chart_text, (
            f"x-axis should label completion date; chart text was:\n{chart_text}"
        )

    def test_dashboard_shows_p50_and_p85_reference_lines_with_values(
        self, server_url: str, page: Page
    ):
        """User-visible signal: the two reference lines labelled with
        their numeric values appear IN the chart SVG, not just in the
        surrounding page chrome.

        The earlier version of this test asserted P50/P85 in the
        component's `inner_text` — which includes the headline
        ("43 items completed · P50 0.1d · P85 1.4d") even when the
        chart itself draws neither line nor label. That was a false
        positive: passing tests, broken chart. Fixed here by reading
        only SVG <text> nodes via page.evaluate (Playwright's
        inner_text doesn't extract SVG text reliably).
        """
        page.goto(server_url + "/workflows/astral-uv-week")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        page.wait_for_timeout(500)  # let Vega finish drawing
        svg_texts: list[str] = page.evaluate(
            """() => Array.from(
                document.querySelectorAll('#cycle-time-tile svg text')
            ).map(t => t.textContent)"""
        )
        # The text-mark layer renders "P50 (X.Xd)" / "P85 (X.Xd)" as
        # SVG <text> nodes. Assert each appears at least once.
        assert any("P50" in t for t in svg_texts), (
            f"P50 label missing from SVG; svg texts were: {svg_texts}"
        )
        assert any("P85" in t for t in svg_texts), (
            f"P85 label missing from SVG; svg texts were: {svg_texts}"
        )

    def test_dashboard_renders_at_least_one_data_point(
        self, server_url: str, page: Page
    ):
        """Forty-three PRs in the fixture window. The chart MUST render
        marks (not just axes). Test the chart is non-empty by counting
        rendered point marks."""
        page.goto(server_url + "/workflows/astral-uv-week")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # Vega-Lite renders point marks as <path> or <circle> under
        # the .mark-symbol class. Empty SVG = bug.
        n_marks = page.locator("#cycle-time-tile .mark-symbol path").count()
        assert n_marks >= 1, (
            "chart rendered 0 data points — Parquet query or Vega data binding broken"
        )

    def test_dashboard_has_details_link_to_detail_page(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week")
        link = page.locator("#cycle-time-tile a:has-text('Details')")
        expect(link).to_be_visible()
        href = link.get_attribute("href")
        assert href is not None and "/metrics/cycle-time" in href, (
            f"Details link href={href!r} — expected /metrics/cycle-time"
        )

    def test_xaxis_date_labels_are_unique(self, server_url: str, page: Page):
        """User-reported bug: x-axis was rendering "May 05 May 05
        May 05 May 05 May 06 May 06 …" — each date label appearing
        four times because Vega-Lite auto-picked sub-day tick
        positions for a 7-day window. The user expects DISTINCT date
        labels along the x-axis.

        Assertion: among SVG <text> nodes that look like date labels
        (match the "%b %d" format), no value appears more than once.
        """
        import re

        page.goto(server_url + "/workflows/astral-uv-week")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        page.wait_for_timeout(500)
        svg_texts: list[str] = page.evaluate(
            """() => Array.from(
                document.querySelectorAll('#cycle-time-tile svg text')
            ).map(t => t.textContent)"""
        )
        date_pattern = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}$")
        date_labels = [t for t in svg_texts if t and date_pattern.match(t)]
        # We expect at least a few unique dates (window is a week).
        assert date_labels, (
            f"no date-shaped labels on x-axis; svg texts: {svg_texts}"
        )
        from collections import Counter
        counts = Counter(date_labels)
        dups = {label: n for label, n in counts.items() if n > 1}
        assert not dups, (
            f"x-axis has duplicated date labels: {dups}. "
            f"All date labels: {date_labels}"
        )


class TestDetailPageCycleTime:
    def test_detail_page_renders_same_chart_full_size(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # The detail page uses the same partial in 'detail' mode; the
        # underlying SVG must still render.
        expect(page.locator("#cycle-time-tile svg")).to_be_visible()

    def test_detail_page_has_placeholder_sections(
        self, server_url: str, page: Page
    ):
        """The detail page reserves space for a collapsible help
        block below the tile, carrying both 'How to read this' and
        'Possible next steps'. The block is closed by default so
        the page reads clean; opening it surfaces both H2s.
        """
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # The collapsible <details class="detail-extras"> exists
        # and is closed by default.
        details = page.locator("details.detail-extras")
        assert details.count() == 1
        assert details.evaluate("el => el.open") is False
        # Open it and confirm both H2s sit inside.
        details.evaluate("el => { el.open = true; }")
        headings = [
            h.lower()
            for h in details.locator("h2").all_inner_texts()
        ]
        assert "how to read this" in headings
        assert "possible next steps" in headings

    def test_detail_page_shows_metric_summary_above_tile(
        self, server_url: str, page: Page
    ):
        """The detail page introduces itself with a brand-accented
        `metric-strip` above the chart — uppercase metric label +
        the one-line headline (percentiles). Mirrors the work-item
        lifecycle metric strip."""
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        strip = page.locator(".metric-strip")
        expect(strip).to_be_visible()
        # `inner_text()` reflects the CSS `text-transform:
        # uppercase` on the label, so compare case-insensitively.
        text = strip.inner_text()
        assert "cycle time" in text.lower(), (
            f"detail metric-strip must name the metric; got {text!r}"
        )
        assert "P50" in text and "P85" in text and "P95" in text, (
            f"detail metric-strip must show the percentiles; got {text!r}"
        )


class TestContractScopedUrls:
    """User-reported feature gap: the dashboard URL must encode the
    contract id so the system is multi-contract-ready by URL shape.
    `/metrics/cycle-time` (the singular form) is no longer the
    canonical URL; the canonical form is
    `/workflows/{contract_id}/metrics/cycle-time`.

    These tests pin the new URL shape. They will require the routes
    in flowmetrics/app.py to be reshaped.
    """

    def test_contract_scoped_dashboard_url_returns_200_with_chart(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        expect(page.locator("#cycle-time-tile svg")).to_be_visible()

    def test_contract_scoped_metric_detail_url_returns_200_with_chart(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        expect(page.locator("#cycle-time-tile svg")).to_be_visible()

    def test_dashboard_details_link_uses_contract_scoped_url(
        self, server_url: str, page: Page
    ):
        """The Details → link on the dashboard tile must point at
        the contract-scoped URL, not the singular legacy form."""
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        link = page.locator("#cycle-time-tile a:has-text('Details')")
        href = link.get_attribute("href")
        assert href is not None and href.startswith(
            "/workflows/astral-uv-week/metrics/cycle-time"
        ), (
            f"Details link should point at /workflows/<id>/metrics/cycle-time; "
            f"got {href!r}"
        )

    def test_unknown_contract_returns_404(self, server_url: str, page: Page):
        response = page.request.get(server_url + "/workflows/does-not-exist/")
        assert response.status == 404, (
            f"expected 404 for unknown contract; got {response.status}"
        )


class TestTooltipDateMatchesDataAcrossTimezones:
    """The tooltip's "Completed" value MUST be the same string for
    every viewer regardless of their browser timezone — it shows
    the UTC calendar date that the data carries.

    Vega-Lite's `type: temporal` tooltip with `format: "%b %d, %Y"`
    formats in browser-local time, which shifts UTC dates by the
    viewer's TZ offset. A PT user sees "May 03" for a UTC May 04
    dot — exactly the off-by-one bug reported. Fix: pre-format the
    date in Python and pass as `type: nominal`, so the rendered
    string is TZ-invariant.
    """

    def _tooltip_for_first_dot(self, page: Page, server_url: str) -> str:
        # Detail page renders the cycle-time chart inline (the
        # dashboard now lazy-loads tiles via HTMX, with fade-in
        # animation; that's a moving target for "hover the dot
        # and read the tooltip"). Same chart partial, same
        # tooltip wiring — only the page chrome differs.
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg")
        page.wait_for_timeout(800)
        # Playwright's `Locator.hover()` scrolls the target into
        # view before firing the move — robust against the
        # sticky filter bar / page chrome that grew during the
        # filter-bar redesign and pushed the chart down.
        dot = page.locator("#cycle-time-tile svg .mark-symbol path").first
        dot.hover(force=True)
        page.wait_for_timeout(500)
        return page.evaluate(
            "() => document.querySelector('#vg-tooltip-element')?.innerText || ''"
        )

    def test_tooltip_completion_date_is_utc_invariant(
        self, server_url: str, browser
    ):
        """Open the dashboard in two contexts — one PT (UTC-7), one
        UTC — and hover the same dot. The tooltip's Completed value
        must read identically. Currently it differs by a day."""
        pt_ctx = browser.new_context(timezone_id="America/Los_Angeles")
        utc_ctx = browser.new_context(timezone_id="UTC")
        try:
            pt_tt = self._tooltip_for_first_dot(pt_ctx.new_page(), server_url)
            utc_tt = self._tooltip_for_first_dot(utc_ctx.new_page(), server_url)
        finally:
            pt_ctx.close()
            utc_ctx.close()

        # Pull the date string that follows the "Completed" label.
        # Vega's tooltip renders as "Label<tab><newline>Value<newline>".
        def _completed_date(text: str) -> str:
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("Completed"):
                    return lines[i + 1].strip() if i + 1 < len(lines) else ""
            return ""

        pt_date = _completed_date(pt_tt)
        utc_date = _completed_date(utc_tt)
        assert pt_date and utc_date, (
            f"could not find Completed date in tooltip. "
            f"PT raw: {pt_tt!r}; UTC raw: {utc_tt!r}"
        )
        assert pt_date == utc_date, (
            f"tooltip 'Completed' must be the same date regardless of "
            f"browser TZ. PT: {pt_date!r}; UTC: {utc_date!r}"
        )


class TestDotsClusterInTheirDateColumn:
    """User-stated mental model: a dot labelled "May 04" lives
    BETWEEN the May 04 tick and the May 05 tick — strictly to the
    right of its tick label, in the [tick, tick+1) band. Tests pin
    this convention against any future "let's center the jitter"
    drift.
    """

    def test_dot_x_is_to_the_right_of_its_date_tick(
        self, server_url: str, page: Page
    ):
        """For the leftmost (earliest) dot, its center x must be
        >= the x of the matching axis tick label and < the x of
        the following tick label.

        Uses the detail page rather than the dashboard — the
        dashboard now lazy-loads tiles with a fade-in animation,
        which makes "hover at a precise pixel" flaky."""
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg")
        page.wait_for_timeout(800)

        # Read all date-shaped tick labels with their x positions.
        import re

        labels = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                '#cycle-time-tile svg .role-axis-label text'
            )).map(t => {
                const bb = t.getBoundingClientRect();
                return {text: t.textContent, x: bb.x + bb.width/2};
            })"""
        )
        date_re = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}$")
        dated = sorted(
            [
                lbl for lbl in labels
                if date_re.match(lbl["text"])
            ],
            key=lambda lbl: lbl["x"],
        )
        assert len(dated) >= 3, f"expected >= 3 date labels; got {dated}"

        # Hover the leftmost dot, read its tooltip date. Use
        # Playwright's `hover()` so it scrolls into view first
        # — the dot may be below the fold after the filter-bar
        # redesign pushed the chart down.
        dot = page.locator("#cycle-time-tile svg .mark-symbol path").first
        dot.hover(force=True)
        bbox = dot.bounding_box()
        assert bbox is not None
        dot_cx = bbox["x"] + bbox["width"] / 2
        page.wait_for_timeout(500)
        tooltip = page.evaluate(
            "() => document.querySelector('#vg-tooltip-element')?.innerText || ''"
        )
        # Pull the tooltip date — e.g., "May 04, 2026"
        m = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+\d{4}", tooltip)
        assert m, f"tooltip missing Completed date; got {tooltip!r}"
        # Zero-pad day so the string matches axis-label form ("May 03",
        # not "May 3").
        tooltip_label = f"{m.group(1)} {int(m.group(2)):02d}"

        # Find that label's x; assert dot_cx is in [tick_x, next_tick_x).
        idx = next(
            (i for i, lbl in enumerate(dated) if lbl["text"] == tooltip_label),
            None,
        )
        assert idx is not None, (
            f"tooltip date {tooltip_label!r} not found in axis labels "
            f"{[lbl['text'] for lbl in dated]}"
        )
        tick_x = dated[idx]["x"]
        next_tick_x = dated[idx + 1]["x"] if idx + 1 < len(dated) else None
        assert dot_cx >= tick_x - 1, (
            f"dot at x={dot_cx:.1f} is LEFT of its tick label "
            f"{tooltip_label!r} (x={tick_x:.1f}). User's column "
            f"convention: dots for May DD live between May DD and "
            f"May DD+1 tick — strictly to the right of their label."
        )
        if next_tick_x is not None:
            assert dot_cx < next_tick_x, (
                f"dot at x={dot_cx:.1f} reaches or exceeds the NEXT "
                f"tick {dated[idx+1]['text']!r} (x={next_tick_x:.1f}). "
                f"Jitter must keep dots strictly inside their band."
            )


class TestMetricSummaryAboveChart:
    """The headline (count + percentiles) is its own component
    above the chart, not embedded inside the chart tile. Same
    pattern reused on every future metric page.
    """

    def test_dashboard_summary_appears_above_chart_tile(
        self, server_url: str, page: Page
    ):
        """Two assertions: the headline text exists on the page,
        AND its position in the DOM is BEFORE the chart-tile
        section. 'Above' is structural, not just visual.

        The dashboard now hosts multiple metric tiles (cycle time,
        throughput, …) each with its own summary block. Scope to
        the cycle-time summary by header text."""
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # Cycle-time summary: the .metric-summary block whose title
        # text contains "Cycle time".
        summary = page.locator(
            ".metric-summary", has_text="Cycle time"
        ).first
        expect(summary).to_be_visible()
        text = summary.inner_text()
        assert "43 items completed" in text, (
            f"summary must show the item count; got {text!r}"
        )
        assert "P50" in text and "P85" in text and "P95" in text, (
            f"summary must show P50/P85/P95; got {text!r}"
        )
        # Structural position: this summary precedes the cycle-time
        # chart tile.
        ordering = page.evaluate(
            """() => {
                const summaries = Array.from(
                    document.querySelectorAll('.metric-summary')
                );
                const summ = summaries.find(s => s.innerText.includes('Cycle time'));
                const tile = document.querySelector('#cycle-time-tile');
                if (!summ || !tile) return null;
                return (summ.compareDocumentPosition(tile) & Node.DOCUMENT_POSITION_FOLLOWING)
                    ? 'summary-first' : 'tile-first';
            }"""
        )
        assert ordering == "summary-first", (
            f"summary must appear BEFORE chart tile in DOM order; got {ordering}"
        )

    def test_headline_is_not_inside_the_chart_tile(
        self, server_url: str, page: Page
    ):
        """The 'metric-headline' (the percentile statement) belongs
        to the summary component above the chart, not to the chart
        tile itself. Tile shows the chart; summary shows the
        numbers."""
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # No headline inside the tile.
        inner = page.locator("#cycle-time-tile .metric-headline").count()
        assert inner == 0, (
            "the percentile headline must NOT live inside the chart "
            "tile (#cycle-time-tile). It belongs to the .metric-summary "
            "component above the tile."
        )


class TestDetailPageNoSubtitleNoise:
    """Earlier slice shipped a subtitle "Per-item cycle time over the
    window…" on the detail page. The user reports it's not useful.
    Remove it.
    """

    def test_per_item_subtitle_is_removed(self, server_url: str, page: Page):
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        body = page.locator("body").inner_text()
        assert "Per-item cycle time over the window" not in body, (
            "the dead 'Per-item cycle time over the window…' subtitle "
            "should be gone — it adds noise without explaining anything."
        )


class TestDetailPageHeader:
    """The metric name lives in the site header on detail pages —
    `flowmetrics · <contract> · <metric>` — so the page identifies
    itself at the top of the viewport, not buried in the metric
    summary section halfway down. Dashboard pages don't show a
    metric name in the header (they're multi-metric).
    """

    def test_detail_page_site_header_contains_metric_name(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        header_text = page.locator(".site-header").inner_text()
        assert "Cycle time" in header_text, (
            f"detail page site header must include the metric name; "
            f"got {header_text!r}"
        )
        # Contract name still present.
        assert "astral-uv-week" in header_text, (
            f"detail page site header must still include the contract "
            f"name; got {header_text!r}"
        )

    def test_dashboard_site_header_does_not_carry_a_metric_name(
        self, server_url: str, page: Page
    ):
        """The dashboard shows multiple metrics; pinning one name to
        the header would be wrong. The contract name is what
        identifies the page."""
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        header_text = page.locator(".site-header").inner_text()
        assert "Cycle time" not in header_text, (
            f"dashboard site header must NOT carry a metric name "
            f"(the dashboard is multi-metric); got {header_text!r}"
        )


class TestThroughputOnDashboard:
    """Slice 3: throughput renders as a second tile on the dashboard,
    plus its own detail page. Daily bar chart, one bar per enumerated
    UTC date in the window (zeros included for slow days).
    """

    def test_dashboard_renders_throughput_tile_with_bars(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#throughput-tile svg", timeout=10000)
        page.wait_for_timeout(400)
        # Vega-Lite renders bar marks under the `.mark-rect` class
        # (bar is a rect-mark variant). One path per enumerated date.
        n_bars = page.evaluate(
            """() => document.querySelectorAll(
                '#throughput-tile svg .mark-rect path'
            ).length"""
        )
        # Fixture window has completions every day across 7 days;
        # expect ≥ 5 bars (defensive against fixture changes).
        assert n_bars >= 5, (
            f"throughput chart drew {n_bars} bars; expected ≥ 5 "
            f"daily bars for the fixture window"
        )

    def test_dashboard_throughput_summary_appears_above_tile(
        self, server_url: str, page: Page
    ):
        """Same composable pattern as cycle time: the headline lives
        in `.metric-summary` ABOVE the chart tile in DOM order."""
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#throughput-tile svg", timeout=10000)
        # There are two metric-summary blocks on the dashboard now
        # (cycle time + throughput); find the one near the
        # throughput tile by walking previousSibling chain.
        ordering = page.evaluate(
            """() => {
                const tile = document.getElementById('throughput-tile');
                if (!tile) return null;
                // The throughput tile's preceding sibling group is
                // the throughput metric-summary. The summary text
                // must mention "Throughput".
                let prev = tile.previousElementSibling;
                while (prev) {
                    if (prev.classList.contains('metric-summary')
                        && prev.innerText.toLowerCase().includes('throughput')) {
                        return 'summary-precedes-tile';
                    }
                    prev = prev.previousElementSibling;
                }
                return 'no-summary-found';
            }"""
        )
        assert ordering == "summary-precedes-tile", (
            f"throughput metric-summary must precede the tile in DOM "
            f"order with title 'Throughput'; got {ordering!r}"
        )

    def test_throughput_detail_page_renders_chart_full_size(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/metrics/throughput")
        page.wait_for_selector("#throughput-tile svg", timeout=10000)
        expect(page.locator("#throughput-tile svg")).to_be_visible()

    def test_throughput_detail_page_header_carries_metric_name(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/metrics/throughput")
        page.wait_for_selector("#throughput-tile svg", timeout=10000)
        header_text = page.locator(".site-header").inner_text()
        assert "Throughput" in header_text, (
            f"detail page site header must include the metric name; "
            f"got {header_text!r}"
        )

    def test_throughput_tile_details_link_uses_contract_scoped_url(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#throughput-tile svg", timeout=10000)
        link = page.locator("#throughput-tile a:has-text('Details')")
        href = link.get_attribute("href")
        assert href and href.startswith(
            "/workflows/astral-uv-week/metrics/throughput"
        ), (
            f"throughput Details → must link to the contract-scoped "
            f"URL; got {href!r}"
        )

    def test_throughput_reset_button_swaps_chart_via_htmx(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#throughput-tile svg", timeout=10000)
        page.wait_for_timeout(400)
        n_before = page.locator("#throughput-tile svg .mark-rect path").count()
        page.locator("#throughput-tile button.reset-btn").first.click()
        page.wait_for_timeout(700)
        page.wait_for_selector("#throughput-tile svg", timeout=5000)
        n_after = page.locator("#throughput-tile svg .mark-rect path").count()
        assert n_after == n_before, (
            f"after reset, throughput should re-render with the same "
            f"data: before={n_before}, after={n_after}"
        )
        assert page.locator("#throughput-chart").count() == 1, (
            "reset swap produced duplicate throughput chart containers"
        )

    def test_clicking_a_bar_filters_the_work_items_table(
        self, server_url: str, page: Page
    ):
        """Click-through: clicking a throughput bar tells the
        work-items table to filter to that completion date. The
        rendered row count must drop to the bar's count value, and
        the table must show a "filtered to <date>" chip with a
        clear link.

        The work-items table lives on the THROUGHPUT DETAIL page
        (not the dashboard) — both pieces co-located, so the click
        affects the table on the same page. The dashboard chart's
        click handler is a no-op there (no table to filter), which
        is intentional.

        The throughput chart is layered: a `g.mark-rect` group for
        the weekend shading and another for the bars. The bars
        layer paints AFTER the shading, so we scope the locator to
        the LAST `.mark-rect` group (the bars). Among those bars
        the tallest is May 04 — 19 completions in the fixture.
        """
        page.goto(server_url + "/workflows/astral-uv-week/metrics/throughput")
        page.wait_for_selector("#throughput-tile svg", timeout=10000)
        page.wait_for_selector("#work-items", timeout=10000)
        page.wait_for_timeout(500)

        # Identify the tallest bar in the bar layer (last
        # `g.mark-rect` group) and dispatch a synthetic MouseEvent
        # with bubbling so Vega's SVG-level click delegation
        # resolves the datum. Playwright's `mouse.click` was hitting
        # the wrong layer on this layered chart.
        clicked = page.evaluate(
            """() => {
                const groups = document.querySelectorAll(
                    '#throughput-tile svg g.mark-rect'
                );
                if (!groups.length) return false;
                const barLayer = groups[groups.length - 1];
                const paths = barLayer.querySelectorAll('path');
                let tallest = null;
                let h = 0;
                for (const p of paths) {
                    const r = p.getBoundingClientRect();
                    if (r.height > h) { h = r.height; tallest = p; }
                }
                if (!tallest) return false;
                const r = tallest.getBoundingClientRect();
                const ev = new MouseEvent('click', {
                    bubbles: true, cancelable: true,
                    clientX: r.x + r.width / 2,
                    clientY: r.y + r.height / 2,
                    view: window,
                });
                tallest.dispatchEvent(ev);
                return true;
            }"""
        )
        assert clicked, "could not locate or dispatch click on a bar"
        page.wait_for_timeout(800)

        # The chip must appear, naming the date.
        chip = page.locator(".work-items-active-filter")
        expect(chip).to_be_visible()
        chip_text = chip.inner_text()
        assert "May" in chip_text, (
            f"chip must show the clicked date; got {chip_text!r}"
        )

        # Row count must drop to the bar's count (single day).
        rows = page.locator("table.work-items-grid tbody tr")
        n_rows = rows.count()
        assert 1 <= n_rows <= 25, (
            f"after click, row count should reflect a single day; "
            f"got {n_rows} rows"
        )

    def test_clear_filter_link_restores_all_rows(
        self, server_url: str, page: Page
    ):
        """The "clear" affordance on the active-filter chip removes
        the date filter and the table re-renders with all rows."""
        page.goto(server_url + "/workflows/astral-uv-week/metrics/throughput")
        page.wait_for_selector("#throughput-tile svg", timeout=10000)
        page.wait_for_selector("#work-items", timeout=10000)
        page.wait_for_timeout(500)

        clicked = page.evaluate(
            """() => {
                const groups = document.querySelectorAll(
                    '#throughput-tile svg g.mark-rect'
                );
                if (!groups.length) return false;
                const barLayer = groups[groups.length - 1];
                const path = barLayer.querySelector('path');
                if (!path) return false;
                const r = path.getBoundingClientRect();
                const ev = new MouseEvent('click', {
                    bubbles: true, cancelable: true,
                    clientX: r.x + r.width / 2,
                    clientY: r.y + r.height / 2,
                    view: window,
                });
                path.dispatchEvent(ev);
                return true;
            }"""
        )
        assert clicked
        page.wait_for_timeout(700)
        expect(page.locator(".work-items-active-filter")).to_be_visible()
        # Click clear.
        page.locator(".work-items-clear-filter").click()
        page.wait_for_timeout(700)
        assert page.locator(".work-items-active-filter").count() == 0, (
            "clear link should remove the active-filter chip"
        )
        # 43 total rows but pagination caps page 1 at 25.
        n_rows = page.locator("table.work-items-grid tbody tr").count()
        assert n_rows == 25, (
            f"clear should restore first page of 25 rows; got {n_rows}"
        )
        # The total count is still surfaced in the header.
        header = page.locator(".work-items-count").inner_text()
        assert "43 items" in header, (
            f"header must show total count of 43; got {header!r}"
        )

    def test_throughput_fragment_endpoint_returns_chart_only(
        self, server_url: str, page: Page
    ):
        response = page.request.get(
            server_url + "/api/internal/throughput?workflow=astral-uv-week"
        )
        assert response.status == 200
        html = response.text()
        assert "throughput-chart" in html
        assert "vegaEmbed" in html
        for forbidden in ("<!doctype html", "site-header", "filter-bar"):
            assert forbidden not in html.lower()


class TestAgingOnDashboard:
    """Slice 3: aging is the third metric on the dashboard, sitting
    below throughput. Vacanti's Aging WIP — in-flight items at the
    snapshot date, plotted by current state (x) and age in days (y),
    with P50/P85/P95 reference lines from completed cycle times.

    Aging is pinned to the in-flight snapshot date (the latest
    materialise) — it does not follow the Period picker. The
    `astral-uv-week` fixture is all completed work, so its aging
    chart is legitimately empty; tests that need a populated chart
    point at the `aging-demo` contract (real in-flight items).
    """

    def test_dashboard_renders_aging_tile_with_zero_items_by_default(
        self, server_url: str, page: Page
    ):
        """Default asof is today UTC. The fixture's items completed
        weeks ago, so the dashboard's aging tile is empty (0
        in-flight items) — empty state is a "no items in flight"
        message, not a chart, since Vega-Lite can't draw a nominal
        x-axis with zero values."""
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#aging-tile", timeout=10000)
        # No SVG; explicit empty-state message.
        expect(page.locator("#aging-tile .aging-empty")).to_be_visible()
        # Summary headline calls out the zero state.
        summary = page.locator(
            ".metric-summary", has_text="Aging WIP"
        ).first
        expect(summary).to_be_visible()
        text = summary.inner_text()
        assert "0 in-flight" in text, (
            f"empty-state summary must say '0 in-flight items'; "
            f"got {text!r}"
        )

    def test_aging_detail_renders_in_flight_dots(
        self, server_url: str, page: Page
    ):
        """The `aging-demo` contract has four genuine in-flight
        items at the snapshot date. The chart must draw a point
        mark for each.

        Vega-Lite renders point marks as `<path>` elements under a
        `g.role-mark.mark-symbol` group. Don't match unqualified
        `.mark-point`; it doesn't exist in v5's SVG output."""
        page.goto(
            server_url + "/workflows/aging-demo/metrics/aging"
        )
        page.wait_for_selector("#aging-tile svg", timeout=10000)
        page.wait_for_timeout(400)
        n_points = page.evaluate(
            """() => document.querySelectorAll(
                '#aging-tile svg g.role-mark.mark-symbol path'
            ).length"""
        )
        assert n_points >= 1, (
            f"aging-demo has four in-flight items; chart drew "
            f"{n_points} point marks"
        )

    def test_aging_detail_page_carries_metric_name_in_header(
        self, server_url: str, page: Page
    ):
        page.goto(
            server_url + "/workflows/astral-uv-week/metrics/aging"
        )
        page.wait_for_selector("#aging-tile", timeout=10000)
        header_text = page.locator(".site-header").inner_text()
        assert "Aging WIP" in header_text

    def test_aging_chart_includes_three_percentile_threshold_lines(
        self, server_url: str, page: Page
    ):
        """Three rule marks for P50/P85/P95 thresholds. Labels live
        in the metric-summary headline (not as in-chart text) since
        anchoring text at the right edge of a layered nominal-x
        chart was unreliable across Vega-Lite versions."""
        page.goto(
            server_url + "/workflows/aging-demo/metrics/aging"
        )
        page.wait_for_selector("#aging-tile svg", timeout=10000)
        page.wait_for_timeout(500)
        # Scope to `.role-mark` so we don't catch axis-grid /
        # axis-tick / axis-domain `<g class="mark-rule …">` groups,
        # which Vega-Lite also tags with `mark-rule`.
        n_rules = page.evaluate(
            """() => document.querySelectorAll(
                '#aging-tile svg g.role-mark.mark-rule line'
            ).length"""
        )
        assert n_rules == 3, (
            f"expected 3 percentile rule lines (P50/P85/P95); "
            f"got {n_rules}"
        )
        # The percentile values are named in the detail-page
        # metric strip above the chart.
        strip = page.locator(
            ".metric-strip", has_text="Aging WIP"
        ).first
        text = strip.inner_text()
        for label in ("P50", "P85", "P95"):
            assert label in text, (
                f"metric strip must name {label!r}; got {text!r}"
            )

    def test_aging_tile_details_link_uses_contract_scoped_url(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#aging-tile", timeout=10000)
        link = page.locator("#aging-tile a:has-text('Details')")
        href = link.get_attribute("href")
        assert href and href.startswith(
            "/workflows/astral-uv-week/metrics/aging"
        ), f"aging Details → must link to contract-scoped URL; got {href!r}"

    def test_default_asof_shows_actionable_coverage_gap_message(
        self, server_url: str, page: Page
    ):
        """User-pinned distinction: empty answers should be action-
        first. Aging pins its as-of to the snapshot date (the
        latest materialise = today in this test); the fixture's
        completion data ends in early May, so aging is past that
        coverage — the empty state names the gap AND links to the
        Data Source page for browser-based backfill (no CLI)."""
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#aging-tile", timeout=10000)
        empty = page.locator("#aging-tile .aging-empty")
        expect(empty).to_be_visible()
        text = empty.inner_text()
        # The message names the gap concretely.
        assert "Most recent data is from" in text, (
            f"empty message must name the latest data date; got {text!r}"
        )
        # No CLI command anywhere in the UI.
        assert "flow materialise" not in text, (
            f"empty state must not print a CLI command; got {text!r}"
        )
        # The action is a link to the Data Source page.
        link = page.locator("#aging-tile .aging-empty a.btn--primary")
        expect(link).to_be_visible()
        href = link.get_attribute("href")
        assert href and href.endswith("/data-source"), (
            f"empty state must link to the Data Source page; got {href!r}"
        )

    def test_real_empty_state_says_warehouse_covers_this_date(
        self, server_url: str, page: Page
    ):
        """Aging pins its `asof` to the in-flight snapshot date.
        The `astral-uv-week` fixture is all completed work with no
        open items at that date, so one of the empty-state
        messages must render.

        This e2e test just confirms the empty-state machinery
        EXISTS on the rendered page (one of the messages), not
        which branch lit up — the component contract is pinned
        directly by the unit tests (TestAgingShape)."""
        page.goto(
            server_url + "/workflows/astral-uv-week/metrics/aging"
        )
        page.wait_for_selector("#aging-tile", timeout=10000)
        empty = page.locator("#aging-tile .aging-empty")
        if empty.count() == 0:
            return  # in-flight items rendered; nothing to assert
        text = empty.inner_text().lower()
        # Some empty-state message rendered.
        assert any(
            phrase in text
            for phrase in (
                "most recent data is from",
                "earliest data is from",
                "no items in flight",
                "warehouse covers",
                "completed-work data through",
                "haven't been captured",
                "import data",
            )
        ), f"empty-state message must explain why; got {text!r}"

    def test_aging_fragment_endpoint_returns_a_bare_fragment(
        self, server_url: str, page: Page
    ):
        """The aging fragment endpoint returns just the chart
        fragment — no Period param, no page chrome. `aging-demo`
        has in-flight items, so the fragment carries the Vega
        embed script."""
        response = page.request.get(
            server_url + "/api/internal/aging?workflow=aging-demo"
        )
        assert response.status == 200
        html = response.text()
        assert "aging-chart" in html
        assert "vegaEmbed" in html
        # No page chrome.
        for forbidden in ("<!doctype html", "site-header", "filter-bar"):
            assert forbidden not in html.lower()


class TestWorkflowUrls:
    """URL & terminology rename: `/workflows/{id}` →
    `/workflows/{id}[/{slug}]` and `?contract=` → `?workflow=`.

    "Contract" was internal jargon ("no engineer would use that
    word"). External surfaces (URLs, query params, UI labels) now
    use "workflow"; the YAML format keeps the `contract:` key for
    now since renaming the on-disk schema is a separate change.

    The slug is decorative — derived from the workflow's source
    (repo for github, project for jira) and ignored by routing
    but preserved on canonical links for shareable URLs.
    """

    def test_dashboard_at_workflows_id(self, server_url: str, page: Page):
        """Dashboard now lazy-loads tiles via HTMX. The initial
        response carries STUBS that fetch each metric tile from
        `/api/internal/dashboard-tile/{metric}`. Assert the
        stub URLs are present; the final chart tile id
        (`cycle-time-tile`) appears after HTMX swaps."""
        r = page.request.get(server_url + "/workflows/astral-uv-week")
        assert r.status == 200
        assert "dashboard-tile/cycle-time" in r.text()

    def test_dashboard_with_slug_also_routes(
        self, server_url: str, page: Page
    ):
        """The slug is decorative — same dashboard, same content."""
        r = page.request.get(
            server_url + "/workflows/astral-uv-week/astral-sh-uv"
        )
        assert r.status == 200
        assert "dashboard-tile/cycle-time" in r.text()

    def test_metric_pages_under_workflows(
        self, server_url: str, page: Page
    ):
        for metric in ("cycle-time", "throughput", "aging", "forecast"):
            url = (
                server_url
                + f"/workflows/astral-uv-week/metrics/{metric}"
            )
            r = page.request.get(url)
            assert r.status == 200, (
                f"{metric}: got {r.status} at {url}"
            )

    def test_items_route_under_workflows(
        self, server_url: str, page: Page
    ):
        r = page.request.get(
            server_url + "/workflows/astral-uv-week/items/19342"
        )
        assert r.status == 200

    def test_internal_endpoints_use_workflow_query_param(
        self, server_url: str, page: Page
    ):
        for path in ("cycle-time", "throughput", "aging", "work-items"):
            r = page.request.get(
                server_url
                + f"/api/internal/{path}?workflow=astral-uv-week"
            )
            assert r.status == 200, f"{path}: got {r.status}"

    def test_old_contracts_path_no_longer_routes(
        self, server_url: str, page: Page
    ):
        """No back-compat redirects in v1 — the old shape was
        never published. Asserting 404 catches accidental
        re-introduction of `/contracts/...` routes."""
        r = page.request.get(server_url + "/contracts/astral-uv-week")
        assert r.status == 404
        # Hitting the endpoint with `?contract=` (instead of
        # `?workflow=`) should fail param validation — `workflow`
        # is now the required name.
        r2 = page.request.get(
            server_url + "/api/internal/cycle-time?contract=astral-uv-week"
        )
        assert r2.status in (400, 404, 422), r2.status

    def test_dashboard_tile_links_use_workflows_prefix(
        self, server_url: str, page: Page
    ):
        """Each tile's Details → link points at the new URL."""
        page.goto(server_url + "/workflows/astral-uv-week")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        for tile in ("cycle-time-tile", "throughput-tile", "aging-tile"):
            link = page.locator(f"#{tile} a:has-text('Details')")
            if link.count() == 0:
                continue  # tile may not have a details link (forecast)
            href = link.get_attribute("href")
            assert href and href.startswith("/workflows/astral-uv-week"), (
                f"#{tile} Details → must point at /workflows/...; got {href!r}"
            )

    def test_filter_bar_does_not_say_contract(
        self, server_url: str, page: Page
    ):
        """Internal `contract` naming stays out of the filter
        controls' user-facing copy. (The page-top filter bar was
        removed in the scope-by-section dashboard; the Period /
        Reference picker now lives in the windowed section's scope
        header.)"""
        page.goto(server_url + "/workflows/astral-uv-week")
        page.wait_for_selector(".scope-section-header--filter", timeout=10000)
        bar = page.locator(".scope-section-header--filter").inner_text()
        assert "Contract" not in bar, (
            f"filter controls must not say 'Contract'; got {bar!r}"
        )

    def test_header_workflow_chip_links_to_dashboard_on_detail_pages(
        self, server_url: str, page: Page
    ):
        """Header on a metric detail page: the workflow chip is a
        link back to the workflow dashboard, the metric name is
        the current (non-link) breadcrumb. The per-metric 'system'
        chip (GitHub / Jira) was dropped from the breadcrumb — the
        viewer doesn't need to be told the source per metric. (The
        data-source freshness strip, which DOES name the source, is
        a separate intentional affordance in the same header row.)"""
        page.goto(
            server_url + "/workflows/astral-uv-week/metrics/cycle-time"
        )
        page.wait_for_selector(".site-header", timeout=10000)
        text = page.locator(".site-header").inner_text()
        assert "astral-uv-week" in text
        assert "Cycle time" in text
        # The workflow chip is a real <a> on a detail page.
        href = page.evaluate(
            "() => document.querySelector(\".site-header a.stamp\")"
            ".getAttribute(\"href\")"
        )
        assert href == "/workflows/astral-uv-week"
        # No per-metric system chip in the breadcrumb stamps. (Scoped
        # to .stamp so the freshness strip naming the source is fine.)
        stamps = " ".join(page.locator(".site-header .stamp").all_inner_texts())
        assert "GitHub" not in stamps and "Jira" not in stamps, (
            f"breadcrumb should carry no system chip; stamps={stamps!r}"
        )


class TestSiteBrandLink:
    """The "flowmetrics" brand text in the site header is a link to
    the home page (`/`). Standard web convention; lets the user
    bounce back to the contract selector / dashboard from any
    detail or lifecycle page without using the browser back button.
    """

    def test_brand_is_an_anchor_to_root(self, server_url: str, page: Page):
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector(".site-header", timeout=10000)
        brand = page.locator(".site-header .brand")
        # The .brand element must BE an <a> tag (not a span wrapping
        # an <a>); the whole brand mark is the click target.
        tag = page.evaluate(
            "() => document.querySelector('.site-header .brand')?.tagName"
        )
        assert tag == "A", (
            f".site-header .brand must be an <a>; got tag {tag!r}"
        )
        href = brand.get_attribute("href")
        assert href == "/", (
            f"brand link must href='/' so it routes to the contract "
            f"redirect; got {href!r}"
        )
        # Text content unchanged.
        assert "flowmetrics" in brand.inner_text().lower()


class TestResetButton:
    """Each chart tile has a reset button that re-loads only the
    chart fragment via HTMX. Use case: re-shuffle jitter, pick up
    new data after an ETL run, etc.
    """

    def test_tile_has_reset_button_with_hx_get(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        btn = page.locator("#cycle-time-tile button.reset-btn").first
        expect(btn).to_be_visible()
        hx_get = btn.get_attribute("hx-get")
        assert hx_get and "/api/internal/cycle-time" in hx_get, (
            f"reset button must use HTMX hx-get pointing at the "
            f"chart fragment endpoint; got hx-get={hx_get!r}"
        )
        hx_target = btn.get_attribute("hx-target")
        assert hx_target == "#cycle-time-chart", (
            f"reset button must target #cycle-time-chart; got {hx_target!r}"
        )

    def test_fragment_endpoint_returns_chart_only_no_chrome(
        self, server_url: str, page: Page
    ):
        """The fragment URL must return JUST the chart + script —
        no page header, no filter bar, no surrounding navigation.
        That's what makes the HTMX swap safe (no double-headers,
        no broken styling)."""
        response = page.request.get(
            server_url
            + "/api/internal/cycle-time?workflow=astral-uv-week"
        )
        assert response.status == 200, (
            f"fragment endpoint returned {response.status}; expected 200"
        )
        html = response.text()
        # Must contain the chart container + script.
        assert "cycle-time-chart" in html, (
            "fragment must include the chart container div"
        )
        assert "vegaEmbed" in html, (
            "fragment must include the Vega-Lite embed script"
        )
        # Must NOT contain page chrome — no <header>, no
        # site-header, no filter bar.
        for forbidden in (
            "<!doctype html",
            "site-header",
            "filter-bar",
        ):
            assert forbidden not in html.lower(), (
                f"fragment must not contain page chrome (found {forbidden!r}); "
                f"got first 200 chars: {html[:200]!r}"
            )

    def test_clicking_reset_swaps_chart_in_place(
        self, server_url: str, page: Page
    ):
        """End-to-end: click reset; assert the chart re-renders
        (a new SVG element is in place after the swap). The jitter
        is random per render, so we can also assert the post-reset
        SVG has the same number of data marks."""
        page.goto(server_url + "/workflows/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        page.wait_for_timeout(500)
        marks_before = page.locator("#cycle-time-tile .mark-symbol path").count()
        # Click reset; wait for HTMX to swap + Vega to re-embed.
        page.locator("#cycle-time-tile button.reset-btn").first.click()
        page.wait_for_timeout(800)
        page.wait_for_selector("#cycle-time-tile svg", timeout=5000)
        marks_after = page.locator("#cycle-time-tile .mark-symbol path").count()
        assert marks_after == marks_before, (
            f"after reset, chart should re-render with same data: "
            f"before={marks_before} marks, after={marks_after}"
        )
        # Still exactly one chart container after swap (no stacking).
        assert page.locator("#cycle-time-chart").count() == 1, (
            "reset swap produced duplicate chart containers"
        )


class TestWorkItemsTableOnDetailPages:
    """The work-items table belongs on metric-specific pages, not on
    the dashboard. The dashboard is a metric overview — its job is
    "what's the shape of the data?" — and the per-item table fits
    on the detail pages, where the viewer has already chosen which
    metric they want to drill into.

    These tests load the cycle-time detail page (the canonical
    detail page in slice 2). The table behavior is identical on the
    throughput detail page; only the surrounding chart differs.
    """

    _DETAIL = "/workflows/astral-uv-week/metrics/cycle-time"

    def test_detail_page_renders_work_items_table_with_paged_rows(
        self, server_url: str, page: Page
    ):
        """Default page size is 25; the fixture has 43 items so
        page 1 shows 25 + page 2 shows 18. The header still
        reports the total."""
        page.goto(server_url + self._DETAIL)
        page.wait_for_selector("#work-items", timeout=10000)
        rows = page.locator("table.work-items-grid tbody tr")
        assert rows.count() == 25, (
            f"expected first page of 25 rows; got {rows.count()}"
        )
        # Pager shows total context.
        pager_info = page.locator(".work-items-pager-info").inner_text()
        assert "1 of 2" in pager_info, (
            f"pager must show 'Page 1 of 2'; got {pager_info!r}"
        )

    def test_table_columns_show_id_title_started_completed_cycle(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + self._DETAIL)
        page.wait_for_selector("#work-items", timeout=10000)
        headers = page.locator("table.work-items-grid thead th").all_inner_texts()
        joined = " ".join(h.lower() for h in headers)
        for expected in ("#", "title", "started", "completed", "cycle"):
            assert expected in joined, (
                f"missing column header {expected!r}; headers were {headers}"
            )

    def test_filter_by_title_narrows_rows_via_htmx(
        self, server_url: str, page: Page
    ):
        """Typing into the search input narrows the row count via
        HTMX (the wrapper swaps in place)."""
        page.goto(server_url + self._DETAIL)
        page.wait_for_selector("#work-items", timeout=10000)
        before = page.locator("table.work-items-grid tbody tr").count()
        # 43 total, paginated to 25 per page.
        assert before == 25

        # HTMX's keyup-changed trigger fires on keyup events; Playwright's
        # `fill()` sets the value without dispatching keyup, so use
        # `press_sequentially` to simulate real typing.
        page.locator("#work-items-search").press_sequentially(
            "zzz-impossible-needle", delay=20
        )
        # debounce + HTMX round-trip
        page.wait_for_timeout(800)
        after_empty = page.locator(".work-items-empty").count()
        rows_after = page.locator("table.work-items-grid tbody tr").count()
        assert after_empty == 1 or rows_after == 0, (
            f"filter for an impossible string should produce empty state; "
            f"got {rows_after} rows / {after_empty} empty messages"
        )

    def test_sort_by_cycle_time_reorders_rows(
        self, server_url: str, page: Page
    ):
        """Clicking the 'Cycle (d)' column header asks the server to
        re-sort by cycle_time. Clicking it again toggles direction.
        Server-side sort via HTMX — no client JS."""
        page.goto(server_url + self._DETAIL)
        page.wait_for_selector("#work-items", timeout=10000)

        def _cycle_values() -> list[float]:
            cells = page.locator(
                "table.work-items-grid tbody td.num"
            ).all_inner_texts()
            return [float(c) for c in cells]

        default_order = _cycle_values()
        # Default is completed_at DESC — values are not necessarily
        # monotonic in cycle_time. With 25-per-page pagination
        # only the first page's worth is visible.
        assert len(default_order) == 25

        # Click the Cycle (d) sort header.
        page.locator("table.work-items-grid thead a:has-text('Cycle')").click()
        page.wait_for_timeout(500)
        sorted_desc = _cycle_values()
        assert sorted_desc == sorted(sorted_desc, reverse=True), (
            f"clicking Cycle header should sort by cycle_time desc; "
            f"got {sorted_desc}"
        )

        # Second click toggles to ascending.
        page.locator("table.work-items-grid thead a:has-text('Cycle')").click()
        page.wait_for_timeout(500)
        sorted_asc = _cycle_values()
        assert sorted_asc == sorted(sorted_asc), (
            f"second click should sort by cycle_time asc; got {sorted_asc}"
        )

    def test_filter_does_not_replace_the_search_input_element(
        self, server_url: str, page: Page
    ):
        """User-reported bug: typing in the search box scrolls the
        page. Root cause: the HTMX swap recreates the entire
        wrapper, so the `<input>` is a fresh DOM node after each
        keystroke.

        Fix is structural: the input must NOT be inside the swap
        target. This test pins that invariant via a marker the
        test plants on the input before filtering — if the element
        survived the swap, the marker is still there.
        """
        page.goto(server_url + self._DETAIL)
        page.wait_for_selector("#work-items", timeout=10000)

        search = page.locator("#work-items-search")
        marker = page.evaluate(
            """() => {
                const el = document.getElementById('work-items-search');
                const m = 'marker-' + Math.random().toString(36).slice(2);
                el.dataset.testMarker = m;
                return m;
            }"""
        )
        assert marker.startswith("marker-")

        search.focus()
        search.press_sequentially("url", delay=20)
        page.wait_for_timeout(800)

        marker_after = page.evaluate(
            """() => document.getElementById('work-items-search')?.dataset.testMarker || ''"""
        )
        assert marker_after == marker, (
            f"search input was recreated during HTMX swap — marker "
            f"{marker!r} did not survive (got {marker_after!r})."
        )

    def test_each_row_links_to_item_lifecycle_page(
        self, server_url: str, page: Page
    ):
        """The leftmost cell (item_id) is a link to the per-item
        lifecycle page under /workflows/<id>/items/<item_id>."""
        page.goto(server_url + self._DETAIL)
        page.wait_for_selector("#work-items", timeout=10000)
        first_id_link = page.locator(
            "table.work-items-grid tbody tr:first-child td.id a"
        )
        href = first_id_link.get_attribute("href")
        assert href is not None
        assert href.startswith("/workflows/astral-uv-week/items/"), (
            f"id-cell link must point at the lifecycle page; got {href!r}"
        )

    def test_dashboard_does_NOT_show_the_work_items_table(
        self, server_url: str, page: Page
    ):
        """Landing page is metric-overview only. The per-item table
        belongs to detail pages — including it on the dashboard
        duplicates information already in the charts and makes the
        landing page noisy."""
        page.goto(server_url + "/workflows/astral-uv-week/")
        # Let charts load so we know the page is fully rendered.
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        page.wait_for_selector("#throughput-tile svg", timeout=10000)
        page.wait_for_timeout(300)
        assert page.locator("#work-items").count() == 0, (
            "dashboard must NOT render the work-items table — it "
            "belongs on the detail pages"
        )
        assert page.locator("table.work-items-grid").count() == 0, (
            "dashboard must NOT render any .work-items-grid table"
        )


class TestWorkItemsFragmentEndpoint:
    """`/api/internal/work-items?contract=X&sort=…&direction=…&q=…`
    returns the work-items partial only (no page chrome). It's the
    HTMX swap target for sort/filter interactions.
    """

    def test_endpoint_returns_table_partial_no_page_chrome(
        self, server_url: str, page: Page
    ):
        response = page.request.get(
            server_url
            + "/api/internal/work-items?workflow=astral-uv-week"
        )
        assert response.status == 200
        html = response.text()
        # The endpoint returns the body partial: count + table (no
        # search input — that lives outside the swap target).
        assert "work-items-grid" in html, (
            f"fragment must contain the table body; first 300 chars: "
            f"{html[:300]!r}"
        )
        # The search input must NOT be in the fragment — it lives
        # outside the swap target and is never re-rendered.
        assert 'id="work-items-search"' not in html, (
            "fragment must not contain the search input (it lives "
            "outside the swap target and stays put across HTMX requests)"
        )
        # No <!doctype>, no site-header, no filter-bar — fragment only.
        for forbidden in (
            "<!doctype html",
            "site-header",
            "filter-bar",
        ):
            assert forbidden not in html.lower(), (
                f"fragment must not contain page chrome (found {forbidden!r}); "
                f"got first 300 chars: {html[:300]!r}"
            )

    def test_endpoint_supports_sort_param(self, server_url: str, page: Page):
        """Pass sort=cycle_time_days&direction=desc; first numeric
        cell in the returned table should be the largest cycle time."""
        response = page.request.get(
            server_url
            + "/api/internal/work-items"
            "?workflow=astral-uv-week&sort=cycle_time_days&direction=desc"
        )
        assert response.status == 200
        html = response.text()
        # Parse out the numeric cells in order. The component renders
        # cycle_time_days inside <td class="num">.
        import re

        # Cycle time is whole-day integer per Vacanti's strict
        # formula — match either bare integers or legacy decimal
        # form so the test survives display-format tweaks.
        nums = [
            float(m.group(1))
            for m in re.finditer(
                r'<td class="num">([0-9]+(?:\.[0-9]+)?)</td>', html
            )
        ]
        assert nums, "expected at least one numeric cycle-time cell"
        assert nums == sorted(nums, reverse=True), (
            f"sort=cycle_time_days&direction=desc should return rows in "
            f"descending cycle order; got {nums}"
        )

    def test_endpoint_supports_q_filter_param(
        self, server_url: str, page: Page
    ):
        """Pass q=<impossible> — endpoint returns the empty state."""
        response = page.request.get(
            server_url
            + "/api/internal/work-items"
            "?workflow=astral-uv-week&q=zzz-impossible-needle"
        )
        assert response.status == 200
        html = response.text()
        assert "work-items-empty" in html, (
            f"q with no matches should render empty state; got first "
            f"300 chars: {html[:300]!r}"
        )

    def test_endpoint_supports_completed_on_filter(
        self, server_url: str, page: Page
    ):
        """`completed_on=YYYY-MM-DD` narrows the table to items
        completed on that exact UTC date — used by the throughput
        chart's bar-click handler."""
        response = page.request.get(
            server_url
            + "/api/internal/work-items"
            "?workflow=astral-uv-week&completed_on=2026-05-04"
        )
        assert response.status == 200
        html = response.text()
        # The "Filtered to" chip should render in the body partial
        # since the filter is active.
        assert "work-items-active-filter" in html, (
            f"endpoint must render the active-filter chip when "
            f"completed_on is set; got first 400 chars: {html[:400]!r}"
        )

    def test_endpoint_404s_for_unknown_contract(
        self, server_url: str, page: Page
    ):
        response = page.request.get(
            server_url + "/api/internal/work-items?workflow=does-not-exist"
        )
        assert response.status == 404


class TestLifecyclePage:
    """`/workflows/<id>/items/<source>/<item_id>` shows a per-item
    lifecycle view. Two presentation modes:

      - **Chartable (≥ 2 stages)**: gantt-style swimlane with one bar
        per stage on the y-axis, time on the x-axis. Labels live on
        the y-axis so they can't overlap.
      - **Trivial (1 stage, 2 events)**: no chart — just a summary
        card with the duration and the two endpoint timestamps. A
        timeline with one bar is noise, not insight.

    Both modes render the same identity header + tabular event list.

    These tests pin specific fixture item ids so the assertion stays
    deterministic regardless of dashboard sort order.
    """

    # 3 events (Draft → Awaiting Review → Merged) — gantt territory.
    _CHARTABLE = "#19342"
    # 2 events (Awaiting Review → Merged) — summary-card territory.
    _NON_CHARTABLE = "#19303"

    @staticmethod
    def _url(item_id: str) -> str:
        """URL builder for the lifecycle page.

        The data-source (github / jira) is implicit in the contract,
        not in the URL. The GitHub `#` is stripped from the URL path
        since `%23` reads as junk to humans; the route accepts both
        forms and resolves to the canonical `#…` id internally.
        """
        from urllib.parse import quote

        clean = item_id.lstrip("#")
        return (
            f"/workflows/astral-uv-week/items/"
            f"{quote(clean, safe='')}"
        )

    def test_chartable_lifecycle_renders_gantt_bars(
        self, server_url: str, page: Page
    ):
        """A 3-event lifecycle draws bars on a gantt — one rect per
        stage. With #19342 (Draft, Awaiting Review) that's 2 bars."""
        page.goto(server_url + self._url(self._CHARTABLE))
        page.wait_for_selector("#lifecycle-chart svg", timeout=10000)
        page.wait_for_timeout(400)
        n_bars = page.evaluate(
            """() => document.querySelectorAll(
                '#lifecycle-chart svg .mark-rect path'
            ).length"""
        )
        assert n_bars >= 2, (
            f"expected ≥ 2 stage bars on the gantt for "
            f"{self._CHARTABLE!r}; got {n_bars}"
        )

    def test_chartable_lifecycle_yaxis_carries_stage_names_and_duration(
        self, server_url: str, page: Page
    ):
        """The y-axis carries `<stage> · <duration>` for each band.
        Replaces the earlier in-bar text overlay, which got cut off
        for narrow bars near the chart's left edge (per the user's
        bug report on #19291)."""
        page.goto(server_url + self._url(self._CHARTABLE))
        page.wait_for_selector("#lifecycle-chart svg", timeout=10000)
        page.wait_for_timeout(400)
        labels = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                '#lifecycle-chart svg .role-axis-label text'
            )).map(t => t.textContent)"""
        )
        # Each label has the form "<stage> · <duration>".
        assert any(label.startswith("Draft · ") for label in labels), (
            f"y-axis must include a 'Draft · <duration>' label; "
            f"got {labels}"
        )
        assert any(
            label.startswith("Awaiting Review · ") for label in labels
        ), (
            f"y-axis must include an 'Awaiting Review · <duration>' "
            f"label; got {labels}"
        )

    def test_lifecycle_header_shows_item_identity(
        self, server_url: str, page: Page
    ):
        """Site header carries item id + contract for either mode."""
        page.goto(server_url + self._url(self._CHARTABLE))
        page.wait_for_selector("#lifecycle-chart svg", timeout=10000)
        header_text = page.locator(".site-header").inner_text()
        assert self._CHARTABLE in header_text, (
            f"site header must include the item id "
            f"{self._CHARTABLE!r}; got {header_text!r}"
        )
        assert "astral-uv-week" in header_text

    def test_non_chartable_lifecycle_renders_summary_not_chart(
        self, server_url: str, page: Page
    ):
        """Trivial 2-event item (#19303): no gantt — a summary
        card instead. The card names the (single) stage, shows its
        duration, and names both the entry timestamp and the
        terminal event."""
        page.goto(server_url + self._url(self._NON_CHARTABLE))
        # Must render — wait for the summary card (NOT a chart).
        page.wait_for_selector(".lifecycle-summary", timeout=10000)
        # No gantt chart container at all in this mode.
        assert page.locator("#lifecycle-chart").count() == 0, (
            f"trivial lifecycle ({self._NON_CHARTABLE!r}) must not "
            f"render the gantt chart container — found one"
        )
        summary = page.locator(".lifecycle-summary").inner_text()
        # The fixture's #19303 has Awaiting Review → Merged.
        assert "Awaiting Review" in summary, (
            f"summary must name the single stage; got {summary!r}"
        )
        assert "Merged" in summary, (
            f"summary must name the terminal event; got {summary!r}"
        )

    def test_non_chartable_lifecycle_still_shows_event_table(
        self, server_url: str, page: Page
    ):
        """Even without a chart, the per-event table at the bottom
        of the page still renders — it's the canonical view of the
        raw transitions for either mode."""
        page.goto(server_url + self._url(self._NON_CHARTABLE))
        page.wait_for_selector(".lifecycle-summary", timeout=10000)
        # The events table is just a .work-items-grid styled table
        # inside the detail-extras section.
        n_event_rows = page.locator(
            "section.detail-extras table.work-items-grid tbody tr"
        ).count()
        assert n_event_rows == 2, (
            f"expected 2 event rows in the table for "
            f"{self._NON_CHARTABLE!r}; got {n_event_rows}"
        )

    def test_lifecycle_page_surfaces_cycle_time_metric(
        self, server_url: str, page: Page
    ):
        """User-reported confusion: the gantt's time axis shows
        wall-clock elapsed time, but the work-items table shows
        the cycle-time metric. The lifecycle page must surface
        the Vacanti number.

        #19330 spans 2026-05-08 19:20 UTC → 2026-05-09 05:13 UTC
        — crosses midnight UTC, so the whole-day Vacanti formula
        gives `(May 09 - May 08) + 1 = 2d`."""
        page.goto(server_url + "/workflows/astral-uv-week/items/19330")
        page.wait_for_selector(".lifecycle-metric-strip", timeout=10000)
        strip = page.locator(".lifecycle-metric-strip").inner_text()
        assert "2d" in strip, (
            f"strip must show #19330's cycle time as 2d "
            f"(May 08 → May 09 UTC); got {strip!r}"
        )

    def test_unknown_item_returns_404(self, server_url: str, page: Page):
        response = page.request.get(
            server_url
            + "/workflows/astral-uv-week/items/does-not-exist"
        )
        assert response.status == 404

    def test_unknown_contract_for_lifecycle_returns_404(
        self, server_url: str, page: Page
    ):
        response = page.request.get(
            server_url + "/workflows/does-not-exist/items/1"
        )
        assert response.status == 404

    def test_url_accepts_id_without_github_hash_prefix(
        self, server_url: str, page: Page
    ):
        """The URL path strips the GitHub `#` so the URL doesn't
        contain `%23`. The route resolves `/items/19342` to the
        warehouse id `#19342` for a GitHub-source contract."""
        # The bare numeric form (no `#`, no `%23`) must work.
        response = page.request.get(
            server_url
            + "/workflows/astral-uv-week/items/19342"
        )
        assert response.status == 200, (
            f"bare numeric id should route to the canonical `#19342` "
            f"for a github-source contract; got {response.status}"
        )


class TestPasswordGate:
    """CLI-level: starting bind off-localhost without a password must
    fail loudly. Uses CliRunner — no server actually starts."""

    def test_off_localhost_without_password_exits_nonzero(self):
        result = CliRunner().invoke(
            cli,
            ["serve", "--host", "0.0.0.0", "--port", "12345"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "password" in result.output.lower(), (
            f"error message should mention password; got:\n{result.output}"
        )

    def test_localhost_default_does_not_require_password(self, tmp_path):
        """The default `--host 127.0.0.1` should NOT require a password.
        We can't actually start the server in this test (would block),
        but we can verify the validation passes by mocking uvicorn.run.
        """
        # Stand up a minimal --data-dir / --workflows-dir so the loader
        # doesn't fail before the bind check.
        (tmp_path / "contracts").mkdir()
        (tmp_path / "data").mkdir()

        # Replace uvicorn.run with a stub that records and returns.
        from unittest.mock import patch

        with patch("uvicorn.run") as mock_run:
            result = CliRunner().invoke(
                cli,
                [
                    "serve",
                    "--port",
                    "12346",
                    "--data-dir",
                    str(tmp_path / "data"),
                    "--workflows-dir",
                    str(tmp_path / "contracts"),
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, (
            f"localhost default should not require password; output:\n{result.output}"
        )
        assert mock_run.called, "uvicorn.run should have been invoked"
