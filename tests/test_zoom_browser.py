"""Browser-based regression test: actually dispatch wheel + drag
events in Chromium and verify chart scale domains change.

Catches the regression where bind:scales is in the spec but
view.fill is null (so the view rect doesn't capture pointer
events on empty plot area), making zoom dead unless cursor is
exactly on a mark. Spec-shape tests (test_vega_specs.py) can't
catch this — they only assert structure.

Marked `browser` so the default `pytest` run skips this (browser
launch is slow + needs chromium installed). Run explicitly:

    uv run pytest -m browser

Prerequisite once: `uv run playwright install chromium`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright  # noqa: E402

pytestmark = pytest.mark.browser


def _axis_labels(page, container_id: str) -> list[str]:
    """Read x-axis tick label texts — proxy for the active scale domain."""
    return page.evaluate(
        f"""() => Array.from(
            document.querySelectorAll('#{container_id} .role-axis-label text')
        ).map(t => t.textContent)"""
    )


def _render_chart_html(tmp_path: Path, chart_command: str) -> Path:
    """Run `flow <chart_command>` against the test fixture cache and
    write HTML to tmp. We use astral-sh/uv's pre-populated cache so
    the test doesn't hit the network."""
    import subprocess
    out_html = tmp_path / "chart.html"
    cmd = [*chart_command.split(), "--format", "html", "--output", str(out_html)]
    subprocess.run(
        ["uv", "run", "flow", *cmd],
        cwd=Path(__file__).parent.parent,
        check=True,
        capture_output=True,
    )
    return out_html


def _wheel_changes_axis(html_path: Path, container_id: str) -> tuple[bool, list, list]:
    """Returns (changed, before, after) — whether wheel-zoom over empty
    plot area changes the axis labels."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"file://{html_path}")
        page.wait_for_selector(f"#{container_id} svg", timeout=15000)
        page.wait_for_timeout(2000)
        before = _axis_labels(page, container_id)
        # Scroll the chart into view so its bounding box is within the
        # viewport — otherwise mouse.wheel fires at off-screen
        # coordinates and Vega's wheel handler never sees it. This
        # matters for tall charts (efficiency can be 1000+ px tall).
        page.locator(f"#{container_id}").scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        box = page.locator(f"#{container_id}").bounding_box()
        viewport = page.viewport_size
        # Pick a y inside the viewport AND inside the chart's box.
        ymin = max(box["y"], 50)
        ymax = min(box["y"] + box["height"], viewport["height"] - 50)
        cx = box["x"] + box["width"] / 2
        cy = (ymin + ymax) / 2
        page.mouse.move(cx, cy)
        page.wait_for_timeout(200)
        for _ in range(8):
            page.mouse.wheel(0, -150)
            page.wait_for_timeout(80)
        page.wait_for_timeout(800)
        after = _axis_labels(page, container_id)
        browser.close()
        return (before != after, before, after)


def _drag_changes_axis(html_path: Path, container_id: str) -> tuple[bool, list, list]:
    """Returns (changed, before, after) — whether click-drag over empty
    plot area pans the chart."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"file://{html_path}")
        page.wait_for_selector(f"#{container_id} svg", timeout=15000)
        page.wait_for_timeout(2000)
        before = _axis_labels(page, container_id)
        box = page.locator(f"#{container_id}").bounding_box()
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.move(cx - 100, cy)
        page.mouse.down()
        page.mouse.move(cx + 150, cy, steps=15)
        page.mouse.up()
        page.wait_for_timeout(800)
        after = _axis_labels(page, container_id)
        browser.close()
        return (before != after, before, after)


# ---------------------------------------------------------------------------
# Use existing pre-rendered samples as input (avoids re-running the CLI per
# test, which would be slow + need cache-window-recovery wiring).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent

CHARTS = [
    ("scatterplot.html", "scatterplot-chart"),
    ("cfd.html", "cfd-chart"),
    ("forecast-when-done.html", "whendone-chart"),
    ("forecast-how-many.html", "howmany-chart"),
    ("aging.html", "aging-chart"),
    # efficiency.html intentionally omitted: its X scale is locked at
    # [0, 100] (the full FE percentage range — that's the chart's
    # value-add), so wheel-zoom is a deliberate no-op. The zoom param
    # is still in the spec (caught by test_vega_specs.py).
]


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    """Render the sample gallery once per session into a tmp dir —
    NOT the tracked `samples/` tree — and return the astral-sh/uv
    folder. Re-rendering tests the LIVE spec; `FLOWMETRICS_SAMPLES_DIR`
    redirects the output so a test run never dirties the committed
    gallery."""
    import os
    import subprocess
    out = tmp_path_factory.mktemp("samples")
    subprocess.run(
        ["uv", "run", "python", "scripts/generate_samples.py", "--offline"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        env={**os.environ, "FLOWMETRICS_SAMPLES_DIR": str(out)},
    )
    return out / "astral-sh_uv"


@pytest.mark.parametrize("filename,container_id", CHARTS)
def test_wheel_zoom_works_over_empty_plot_area(
    sample_dir, filename, container_id
):
    """The regression: bind:scales on a layered spec was dead over
    empty plot area because view.fill: null meant no event-catcher
    rect. Fix is view.fill: 'transparent'."""
    html = sample_dir / filename
    if not html.exists():
        pytest.skip(f"{filename} not in sample dir")
    changed, before, after = _wheel_changes_axis(html, container_id)
    assert changed, (
        f"Wheel zoom did NOT change axis labels for {filename}.\n"
        f"before ({len(before)}): {before}\nafter  ({len(after)}): {after}\n"
        f"This means bind:scales is not catching wheel events on "
        f"empty plot area. Check view.fill is 'transparent' not null."
    )


# ============================================================
# Render-fidelity regression: catches "vegaEmbed is not defined"
# script-loading races AND chart-spec errors that leave the
# container empty.
# ============================================================


@pytest.mark.parametrize("filename,container_ids", [
    ("scatterplot.html", ["scatterplot-chart"]),
    ("cfd.html", ["cfd-chart"]),
    ("forecast-when-done.html", ["whendone-chart"]),
    ("forecast-how-many.html", ["howmany-chart"]),
    ("efficiency.html", ["efficiency-chart"]),
    # aging.html has TWO charts on one page — the bug that motivated
    # this test was the distribution chart staying blank because
    # the vega-embed script tag sat between the two inline scripts.
    ("aging.html", ["aging-chart", "aging-dist-chart"]),
])
def test_every_chart_actually_renders_svg(
    sample_dir, filename, container_ids
):
    """Each chart container should hold a non-trivial Vega SVG once
    loaded. Catches:
      - vegaEmbed runtime errors ("vegaEmbed is not defined" race
        from script-tag ordering)
      - spec compile errors that leave the container empty
      - CDN load failures
    """
    html = sample_dir / filename
    if not html.exists():
        pytest.skip(f"{filename} not in sample dir")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1500})
        page_errors: list[str] = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.goto(f"file://{html}")
        for cid in container_ids:
            page.wait_for_selector(f"#{cid} svg", timeout=15000)
        page.wait_for_timeout(1500)
        # ANY pageerror is a render failure — the previous narrower
        # filter ("vegaEmbed" or "Cannot") missed "Cycle detected in
        # dataflow graph" from a misused expression. Catch them all.
        assert not page_errors, (
            f"{filename}: chart script errored — {page_errors}"
        )
        # Each container's SVG must have real content (axes + marks).
        for cid in container_ids:
            svg_kids = page.evaluate(
                f"""() => {{
                    const el = document.querySelector('#{cid} svg');
                    return el ? el.children.length : 0;
                }}"""
            )
            assert svg_kids >= 2, (
                f"{filename} / #{cid}: SVG rendered but only "
                f"{svg_kids} children — chart looks empty."
            )
        browser.close()
