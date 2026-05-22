"""Codebase-wide safety net: no chart leaks a browser-local time
formatter into a rendered value.

Why this test exists
--------------------

Vega-Lite has two distinct ways the browser's local timezone can
silently shift a UTC value before display:

1. **`type: temporal` on a tooltip / encoding field.** Tells Vega to
   parse the value as a timestamp and format it via the
   browser-local formatter. UTC May 04 renders as "May 03" for a
   viewer in PT (UTC-7) and "May 04" in UTC. Same data, different
   displays. We hit this once on the cycle-time tooltip.

2. **`timeFormat(...)` inside an axis `labelExpr` (or any other
   Vega expression).** `timeFormat` is the browser-local formatter
   too — same TZ-shift class of bug, different surface. The tooltip
   audit alone won't catch this (it scans tooltip entries only).
   Use Vega's `utcFormat(...)` instead, which ignores browser TZ.

Pair with `flowmetrics.utc_dates` — the runtime utility that
formats dates UTC-anchored before they reach Vega.

How to add coverage for a new chart
-----------------------------------

When a new component renders a Vega-Lite spec, add it to the
`_collect_component_specs` walker below. The test then enforces
both rules for that component automatically.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from datetime import date

from flowmetrics.cli import cli
from flowmetrics.web.components.aging import render as render_aging
from flowmetrics.web.components.cfd import render as render_cfd
from flowmetrics.web.components._vega import to_vega
from flowmetrics.web.components.cycle_time import render as render_cycle_time
from flowmetrics.web.components.forecast import (
    render_how_many as render_forecast_how_many,
    render_when_done as render_forecast_when_done,
)
from flowmetrics.web.components.throughput import render as render_throughput

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    """Materialise the pinned fixture data into a tmp warehouse so
    every component renderer has data to read. Independent of the
    other component-test fixtures so this file stands alone."""
    tmp = Path(tempfile.mkdtemp())
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"
    (contracts_dir / "astral-uv-week.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
                    "name": "astral-uv-week",
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
            "astral-uv-week",
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
    assert res.exit_code == 0, res.output

    con = duckdb.connect(":memory:")
    # Aging reads from both fact tables (work_items for the
    # in-flight set + percentile thresholds; transitions for the
    # current-state lookup). Register both so every component's
    # spec can be rendered for the audit.
    for kind in ("work_items", "transitions"):
        glob = (data_dir / kind / "**" / "*.parquet").as_posix()
        con.execute(
            f"CREATE VIEW {kind} AS "
            f"SELECT * FROM read_parquet('{glob}', hive_partitioning = true)"
        )
    yield con
    con.close()


def _collect_component_specs(warehouse) -> list[tuple[str, dict]]:
    """Render every chart component and return (component_name,
    parsed Vega-Lite spec) tuples. Add new components here when
    they ship."""
    return [
        (
            "cycle_time",
            to_vega(render_cycle_time(warehouse, "astral-uv-week")),
        ),
        (
            "throughput",
            json.loads(
                render_throughput(warehouse, "astral-uv-week").vega_spec_json()
            ),
        ),
        (
            "aging",
            # Pick an asof inside the fixture window so the
            # in-flight set is non-empty and the spec is
            # representative of a real render.
            to_vega(
                render_aging(
                    warehouse, "astral-uv-week", asof=date(2026, 5, 6),
                )
            ),
        ),
        (
            "forecast_when_done",
            json.loads(
                render_forecast_when_done(
                    warehouse,
                    "astral-uv-week",
                    items=20,
                    start_date=date(2026, 5, 11),
                ).vega_spec_json()
            ),
        ),
        (
            "forecast_how_many",
            json.loads(
                render_forecast_how_many(
                    warehouse,
                    "astral-uv-week",
                    start_date=date(2026, 5, 11),
                    end_date=date(2026, 6, 10),
                ).vega_spec_json()
            ),
        ),
        (
            "cfd",
            to_vega(render_cfd(warehouse, "astral-uv-week")),
        ),
    ]


def _walk_tooltip_entries(spec: dict):
    """Yield every tooltip entry across all layers of a Vega-Lite
    spec. Returns dicts each describing one field bound to a
    tooltip."""
    for layer in spec.get("layer", [spec]):
        tooltip = layer.get("encoding", {}).get("tooltip")
        if tooltip is None:
            continue
        entries = tooltip if isinstance(tooltip, list) else [tooltip]
        for entry in entries:
            if isinstance(entry, dict):
                yield entry


def _walk_strings(spec):
    """Yield every string-typed value anywhere in the Vega-Lite spec,
    annotated with the dotted path that reached it. Used by the
    `timeFormat` audit to scan labelExpr / expr / format / any other
    place a Vega expression can live."""

    def _recurse(node, path):
        if isinstance(node, str):
            yield path, node
        elif isinstance(node, dict):
            for k, v in node.items():
                yield from _recurse(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                yield from _recurse(v, f"{path}[{i}]")

    yield from _recurse(spec, "")


class TestNoTemporalTooltips:
    """The single hard rule. Use the runtime utility
    `flowmetrics.utc_dates.to_utc_display_date` to pre-format any
    date you want in a tooltip; bind that string with
    `type: nominal`.
    """

    def test_no_chart_tooltip_uses_type_temporal(self, warehouse):
        for component_name, spec in _collect_component_specs(warehouse):
            for entry in _walk_tooltip_entries(spec):
                assert entry.get("type") != "temporal", (
                    f"chart {component_name!r} has a tooltip entry with "
                    f"type:temporal, which Vega-Lite formats in BROWSER-"
                    f"LOCAL time and produces TZ-shifted display strings "
                    f"for different viewers. Pre-format the date in "
                    f"Python via flowmetrics.utc_dates.to_utc_display_date "
                    f"and bind with type:nominal instead. Offending "
                    f"entry: {entry!r}"
                )


class TestNoBrowserLocalTimeFormatters:
    """`timeFormat(...)` is Vega's browser-local time formatter —
    the axis-side counterpart of the type:temporal tooltip bug. Any
    string anywhere in a spec that calls `timeFormat(` will TZ-shift
    in the viewer's browser. Use `utcFormat(...)` instead (or, even
    better, pre-format the value in Python and bind nominally).
    """

    def test_no_chart_spec_calls_timeFormat(self, warehouse):
        for component_name, spec in _collect_component_specs(warehouse):
            for path, value in _walk_strings(spec):
                assert "timeFormat(" not in value, (
                    f"chart {component_name!r} contains a `timeFormat(...)` "
                    f"call at spec path {path!r}. timeFormat is the "
                    f"BROWSER-LOCAL time formatter and will shift UTC dates "
                    f"by the viewer's timezone — the same class of bug as "
                    f"type:temporal tooltips. Use `utcFormat(...)` instead, "
                    f"or pre-format the value in Python and bind nominally. "
                    f"Offending value: {value!r}"
                )

    def test_no_chart_spec_contains_literal_hsl_color(self, warehouse):
        """Single source of truth: chart colors come from CSS theme
        tokens (`--p-500` etc.) on `:root`, not from literals
        scattered through Python or templates. Specs use
        `__theme:<token>__` placeholders that the in-browser theme
        helper substitutes from CSS variables at embed time.

        This test enforces the rule on every spec returned by a
        component renderer. Templates that emit specs inline (e.g.
        lifecycle_chart.html.jinja) are policed by reading the
        template file directly — see
        `test_no_chart_partial_template_contains_literal_hsl_color`.
        """
        import re

        for component_name, spec in _collect_component_specs(warehouse):
            for path, value in _walk_strings(spec):
                assert not re.search(r"hsla?\(", value), (
                    f"chart {component_name!r} embeds a literal HSL/HSLA "
                    f"color at spec path {path!r}: {value!r}. Use a "
                    f"`__theme:<token>__` placeholder instead so the "
                    f"color resolves from the CSS theme at embed time. "
                    f"The token list lives in _base.html.jinja's "
                    f"`flowmetricsTheme` object."
                )

    def test_no_chart_partial_template_contains_literal_hsl_color(self):
        """Mirror audit for templates that emit Vega-Lite specs
        inline (lifecycle currently). The partial files must not
        contain `hsl(...)` or `hsla(...)` literals — use
        `__theme:<token>__` placeholders that `window.applyTheme`
        substitutes at embed time."""
        import re
        from pathlib import Path

        templates_dir = (
            Path(__file__).parent.parent
            / "src"
            / "flowmetrics"
            / "web"
            / "templates"
            / "_partials"
        )
        offenders: list[tuple[str, int, str]] = []
        for path in templates_dir.glob("*_chart*.jinja"):
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                # Skip Jinja comments and `//`-style JS comments —
                # documentary references like `// --p-500` are fine.
                code, _, _ = line.partition("//")
                code = code.split("{#", 1)[0]
                if re.search(r"hsla?\(", code):
                    offenders.append((path.name, lineno, line.strip()))
        assert not offenders, (
            "chart partial templates contain literal HSL/HSLA colors. "
            "Replace with `__theme:<token>__` placeholders that the "
            "client-side theme helper substitutes at embed time. "
            f"Offenders:\n"
            + "\n".join(f"  {n}:{ln}: {ll}" for n, ln, ll in offenders)
        )

    def test_audit_catches_a_deliberately_bad_spec(self):
        """Sanity check that the walker actually finds `timeFormat(`
        in a contrived spec. Guards against the audit silently
        no-op-ing on string-walking refactors."""
        bad = {
            "encoding": {
                "x": {
                    "field": "date_iso",
                    "type": "nominal",
                    "axis": {
                        "labelExpr": "timeFormat(datetime(datum.value), '%b %d')",
                    },
                }
            }
        }
        hits = [
            (path, value)
            for path, value in _walk_strings(bad)
            if "timeFormat(" in value
        ]
        assert hits, (
            "audit walker failed to find a `timeFormat(` call inside an "
            "axis labelExpr — the walker is broken; downstream production "
            "audits would silently pass dangerous specs"
        )
