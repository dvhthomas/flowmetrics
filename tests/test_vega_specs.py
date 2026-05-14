"""Behavioural spec for the Vega-Lite spec generators.

The spec generators turn a Report into a JSON-serializable dict that
Vega-Embed can render in the browser. Tests assert structural shape
(marks, encoding channels, data values, layered percentile rules) so
the rendered chart is verifiable without a real browser.

Plus one integration-style test (`TestSpecCompilesViaVegaLite`) that
actually runs the vendored Vega-Lite over the spec via Node, catching
compile-time errors like 'Duplicate signal name: zoom_tuple' that
unit tests of spec shape would miss.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from flowmetrics.aging import AgingItem
from flowmetrics.renderers import vega_specs
from flowmetrics.report import AgingInput, AgingReport


def _interp():
    from flowmetrics.report import Interpretation

    return Interpretation(
        headline="h", key_insight="k", next_actions=["a"], caveats=["c"]
    )


def _aging_report(items: list[AgingItem]) -> AgingReport:
    return AgingReport(
        input=AgingInput(
            repo="acme/widget",
            asof=date(2026, 5, 14),
            workflow=("Awaiting Review", "Approved"),
            history_start=date(2026, 4, 14),
            history_end=date(2026, 5, 13),
            offline=False,
        ),
        items=items,
        cycle_time_percentiles={50: 1.7, 70: 5.4, 85: 17.8, 95: 57.4},
        completed_count=100,
        interpretation=_interp(),
        generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )


def _item(item_id: str, state: str, age: int, pr_url: str | None = None) -> AgingItem:
    return AgingItem(
        item_id=item_id,
        title=f"PR {item_id}",
        current_state=state,
        age_days=age,
        pr_url=pr_url,
    )


class TestAgingSpec:
    def test_top_level_shape_is_vega_lite_v5(self):
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 100)]))
        assert spec["$schema"].startswith("https://vega.github.io/schema/vega-lite/")

    def test_data_values_carry_items_with_required_fields(self):
        items = [
            _item("#1", "Awaiting Review", 3, pr_url="https://x/1"),
            _item("#2", "Approved", 10),
        ]
        spec = vega_specs.aging_spec(_aging_report(items))
        # The chart is a layered spec — circles + percentile rules.
        # The circle layer's data carries the items.
        circle_layer = next(
            layer for layer in spec["layer"]
            if layer["mark"].get("type") == "circle"
            or layer["mark"] == "circle"
        )
        values = circle_layer["data"]["values"]
        assert len(values) == 2
        ids = {v["item_id"] for v in values}
        assert ids == {"#1", "#2"}
        # Each value carries the fields the encoding will use.
        v1 = next(v for v in values if v["item_id"] == "#1")
        assert v1["age_days"] == 3
        assert v1["current_state"] == "Awaiting Review"
        assert v1["pr_url"] == "https://x/1"
        assert v1["title"] == "PR #1"

    def test_circles_have_tooltip_and_href_channels(self):
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 100)]))
        circle_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "circle"
        )
        enc = circle_layer["encoding"]
        # X = workflow state (categorical), Y = age in days.
        assert enc["x"]["field"] == "current_state"
        assert enc["x"]["type"] == "nominal"
        assert enc["y"]["field"] == "age_days"
        assert enc["y"]["type"] == "quantitative"
        # Tooltip shows the metadata.
        tooltip_fields = {t["field"] for t in enc["tooltip"]}
        assert {"item_id", "title", "age_days", "current_state"} <= tooltip_fields
        # Click navigates to the PR URL (Vega-Lite's `href` channel).
        assert enc["href"]["field"] == "pr_url"

    def test_x_axis_is_titled_wip_stage(self):
        """The X axis names workflow stages, not 'current_state' (raw
        field name) and not blank. 'WIP Stage' matches the chart's
        Vacanti framing."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 5)]))
        circle_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "circle"
        )
        assert circle_layer["encoding"]["x"]["axis"]["title"] == "WIP Stage"

    def test_per_state_count_labels_layer_present(self):
        """Each column gets a count + share label at the top, restoring
        what the matplotlib chart had (e.g. 'WIP: 2 (40%)')."""
        spec = vega_specs.aging_spec(_aging_report([
            _item("#1", "Awaiting Review", 5),
            _item("#2", "Awaiting Review", 10),
            _item("#3", "Approved", 30),
        ]))
        text_layers = [
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "text"
        ]
        # At least one text layer must carry per-state counts. (There's
        # also the percentile-line label text layer; one of them must
        # be the per-state header.)
        count_layer = next(
            (lyr for lyr in text_layers
             if any("count" in str(v).lower()
                    for v in lyr.get("data", {}).get("values", []) for _ in [None])
             or any("WIP" in str(v) for v in lyr.get("data", {}).get("values", []))),
            None,
        )
        assert count_layer is not None, "expected per-state count header layer"
        values = count_layer["data"]["values"]
        # Two rows: one per workflow state. The fixture has 2 in
        # Awaiting Review and 1 in Approved.
        by_state = {v["current_state"]: v for v in values}
        assert by_state["Awaiting Review"]["count"] == 2
        assert by_state["Approved"]["count"] == 1

    def test_x_axis_sort_matches_workflow_order(self):
        """Vega-Lite default sorts nominal axes alphabetically. The chart
        must respect the user's workflow ordering (left = earliest,
        right = latest) so Aging columns mirror Vacanti's convention."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Approved", 5)]))
        circle_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "circle"
        )
        x = circle_layer["encoding"]["x"]
        assert x["sort"] == ["Awaiting Review", "Approved"]

    def test_x_scale_domain_forces_all_workflow_states_to_appear(self):
        """Even if the data has zero items in a workflow state, that
        column must still appear on the X-axis. Otherwise an empty
        Approved column looks like it doesn't exist in the workflow,
        which is misleading. `sort` alone is not enough; we set
        scale.domain to the workflow tuple explicitly."""
        # Only one state has data, but two states are in the workflow.
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 5)]))
        circle_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "circle"
        )
        x = circle_layer["encoding"]["x"]
        assert x["scale"]["domain"] == ["Awaiting Review", "Approved"]

    def test_zoom_param_lives_on_circle_layer_not_top_level(self):
        """Vega-Lite compiles top-level params per-layer in a layered
        spec, producing duplicate signal names like `zoom_tuple` at
        runtime: 'Duplicate signal name: zoom_tuple'. Putting params on
        a single layer (the circles) gives one signal; scales remain
        shared across layers by Vega-Lite default."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 5)]))
        # Top-level params must NOT exist on a layered spec — otherwise
        # the chart will throw at render time.
        assert "params" not in spec, (
            "params at top level of a layered Vega-Lite spec causes "
            "'Duplicate signal name' errors — put them on one layer."
        )
        circle_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "circle"
        )
        assert "params" in circle_layer

    def test_y_axis_zoom_and_pan_via_interval_param_bound_to_scales(self):
        """An interval selection bound to scales gives drag-to-pan and
        scroll-to-zoom. We constrain it to the Y axis only — the X axis
        is nominal (workflow columns) so panning it makes no sense."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 5)]))
        circle_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "circle"
        )
        zoom_params = [p for p in circle_layer["params"] if p.get("bind") == "scales"]
        assert len(zoom_params) == 1
        select = zoom_params[0]["select"]
        assert isinstance(select, dict), "expected explicit interval-select object"
        assert select.get("type") == "interval"
        # Restricted to Y; X is nominal.
        assert "y" in select.get("encodings", [])
        assert "x" not in select.get("encodings", [])

    def test_jitter_transform_and_x_offset_distribute_points_within_band(self):
        """Without jitter, all points in a column stack vertically at the
        column center. We add a `random()`-derived field per item and
        use it on the xOffset channel so points spread horizontally
        across the band width."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 5)]))
        circle_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "circle"
        )
        # The transform creates the jitter field.
        transforms = circle_layer.get("transform", [])
        jitter_xform = next(
            (t for t in transforms if t.get("as") == "jitter"), None
        )
        assert jitter_xform is not None
        assert "random()" in jitter_xform["calculate"]
        # The encoding uses it on xOffset.
        xoffset = circle_layer["encoding"]["xOffset"]
        assert xoffset["field"] == "jitter"
        assert xoffset["type"] == "quantitative"

    def test_percentile_rules_consolidated_in_one_layer_with_color_encoding(self):
        """All four percentile thresholds share one rule layer so a
        single color scale (yellow-low-risk → red-high-risk) can encode
        them. Four separate layers can't share a color legend."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 100)]))
        rule_layers = [
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rule"
        ]
        # Exactly one rule layer, containing all four percentile rows.
        assert len(rule_layers) == 1
        rule_layer = rule_layers[0]
        rows = rule_layer["data"]["values"]
        ys = sorted(r["y"] for r in rows)
        assert ys == [1.7, 5.4, 17.8, 57.4]
        # Each row has a percentile label so the color/text channels
        # can encode it.
        pct_labels = sorted(r["pct"] for r in rows)
        assert pct_labels == ["P50", "P70", "P85", "P95"]
        # Color is encoded so Vega renders thresholds visibly distinct.
        assert "color" in rule_layer["encoding"]
        color = rule_layer["encoding"]["color"]
        assert color["field"] == "pct"

    def test_p85_line_is_solid_and_heavier_others_remain_dashed(self):
        """P85 is the forecast line in Vacanti's framing — make it stand
        out: solid + thicker. P50/P70/P95 stay dashed/lighter so the
        forecast threshold reads at a glance."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 100)]))
        rule_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rule"
        )
        enc = rule_layer["encoding"]
        # `strokeDash` is conditional on the pct field — non-dashed
        # when pct === "P85", dashed otherwise.
        assert "strokeDash" in enc
        sd = enc["strokeDash"]
        # Either a conditional (dict with "condition") or a transform
        # — both are acceptable, but there must be SOME mechanism.
        assert "condition" in sd or "field" in sd
        # `size` similarly varies: P85 gets a heavier stroke.
        assert "size" in enc
        sz = enc["size"]
        assert "condition" in sz or "field" in sz

    def test_rule_layers_sit_behind_circles_so_dots_overlay_thresholds(self):
        """Layer order: percentile rules paint BEFORE circles, so the
        dots overlay the thresholds. Threshold lines remain visible in
        empty regions (where there are no items) and circles sit on
        top elsewhere — keeps the chart legible."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 100)]))
        layer_kinds = [
            (lyr["mark"].get("type") if isinstance(lyr["mark"], dict) else lyr["mark"])
            for lyr in spec["layer"]
        ]
        i_first_rule = layer_kinds.index("rule")
        i_circle = layer_kinds.index("circle")
        assert i_first_rule < i_circle, "rules must paint before circles"

    def test_chart_has_transparent_background_and_no_view_fill(self):
        """Vega-Lite defaults render the view (chart plot area) with a
        subtle fill that can show through and look like a tint. Make
        background + view.fill explicit (transparent / null) so the
        chart is on plain white regardless of theme defaults."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 100)]))
        assert spec.get("background") == "transparent"
        view = spec.get("config", {}).get("view", {})
        assert view.get("fill") is None
        assert "stroke" in view  # explicit stroke (even if subtle)

    def test_percentile_thresholds_above_data_range_are_dropped(self):
        """If the highest in-flight item is at 100d but P95 of recent
        completers is 600d, drawing P95 forces the Y axis out to 600
        — wasting ~5x of vertical canvas. Drop thresholds well above
        the actual data so the chart fits what's there."""
        # P50=2, P70=5, P85=20, P95=200; data maxes at 30d so P95 is
        # well above the data range and should be filtered out.
        report = AgingReport(
            input=AgingInput(
                repo="acme/widget",
                asof=date(2026, 5, 14),
                workflow=("State A",),
                history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13),
                offline=False,
            ),
            items=[_item("#1", "State A", 30), _item("#2", "State A", 10)],
            cycle_time_percentiles={50: 2.0, 70: 5.0, 85: 20.0, 95: 200.0},
            completed_count=100,
            interpretation=_interp(),
            generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        spec = vega_specs.aging_spec(report)
        rule_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rule"
        )
        ys = {row["y"] for row in rule_layer["data"]["values"]}
        # P50/P70/P85 are within or near the data range; P95 is way out.
        assert 2.0 in ys and 5.0 in ys and 20.0 in ys
        assert 200.0 not in ys

    def test_percentile_thresholds_just_above_data_are_kept(self):
        """Some headroom — a threshold sitting just above the highest
        data point IS useful (shows what you're approaching). Drop only
        thresholds that exceed ~1.5x the max age."""
        # Max age 100; P85 at 110 (just above) should stay; P95 at 250
        # (way above) should drop.
        report = AgingReport(
            input=AgingInput(
                repo="acme/widget",
                asof=date(2026, 5, 14),
                workflow=("State A",),
                history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13),
                offline=False,
            ),
            items=[_item("#1", "State A", 100)],
            cycle_time_percentiles={50: 10.0, 70: 50.0, 85: 110.0, 95: 250.0},
            completed_count=100,
            interpretation=_interp(),
            generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        spec = vega_specs.aging_spec(report)
        rule_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rule"
        )
        ys = {row["y"] for row in rule_layer["data"]["values"]}
        assert 110.0 in ys  # just above max — kept
        assert 250.0 not in ys  # >2x max — dropped

    def test_no_background_rect_for_p95_band(self):
        """Earlier iterations painted the area above P95 with a light-red
        tint. Removed: per Tufte 'just show the lines, not paint half
        the chart a different color'. The threshold encoding is the
        line + label, not the background."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 100)]))
        rect_layers = [
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rect"
        ]
        assert rect_layers == []

    def test_no_danger_rect_when_p95_is_zero(self):
        """No percentile data → no rect (nothing to tint)."""
        report = AgingReport(
            input=AgingInput(
                repo="acme/widget",
                asof=date(2026, 5, 14),
                workflow=("Awaiting Review",),
                history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13),
                offline=False,
            ),
            items=[_item("#1", "Awaiting Review", 3)],
            cycle_time_percentiles={50: 0.0, 70: 0.0, 85: 0.0, 95: 0.0},
            completed_count=0,
            interpretation=_interp(),
            generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        spec = vega_specs.aging_spec(report)
        rect_layers = [
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rect"
        ]
        assert rect_layers == []

    def test_percentile_lines_have_direct_text_labels(self):
        """Each percentile rule has a matching text mark printing
        `P85 (17.8d)` directly on the chart, so the reader doesn't have
        to chase a legend."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 100)]))
        # Find the percentile-label text layer specifically (by content,
        # not by being the only text layer — there's also a per-state
        # header layer now).
        text_layers = [
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "text"
        ]
        pct_layer = next(
            (lyr for lyr in text_layers
             if any(str(v.get("label", "")).startswith("P")
                    for v in lyr.get("data", {}).get("values", []))),
            None,
        )
        assert pct_layer is not None
        rows = pct_layer["data"]["values"]
        labels = sorted(r["label"] for r in rows)
        assert labels == ["P50 (1.7d)", "P70 (5.4d)", "P85 (17.8d)", "P95 (57.4d)"]

    def test_zero_percentiles_omit_rules(self):
        """When there are no completed items in the training window,
        all percentiles are 0; drawing rules at y=0 is useless visual
        clutter, so we omit them."""
        report = AgingReport(
            input=AgingInput(
                repo="acme/widget",
                asof=date(2026, 5, 14),
                workflow=("Awaiting Review",),
                history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13),
                offline=False,
            ),
            items=[_item("#1", "Awaiting Review", 3)],
            cycle_time_percentiles={50: 0.0, 70: 0.0, 85: 0.0, 95: 0.0},
            completed_count=0,
            interpretation=_interp(),
            generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        spec = vega_specs.aging_spec(report)
        rule_layers = [
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rule"
        ]
        assert rule_layers == []


# ---------------------------------------------------------------------------
# End-to-end: actually compile the spec via Node + vendored Vega-Lite.
#
# Pure unit tests of spec shape can miss whole classes of bug — e.g.
# "Duplicate signal name: zoom_tuple" only surfaces when Vega-Lite
# compiles the spec into a Vega runtime spec. This suite shells out to
# scripts/check_vega_spec.js and exercises the real compiler.
# ---------------------------------------------------------------------------


_REPO_ROOT = Path(__file__).resolve().parent.parent
_CHECKER = _REPO_ROOT / "scripts" / "check_vega_spec.js"


def _compile_via_node(spec: dict) -> tuple[bool, str, list]:
    """Run the vendored Vega-Lite compiler over `spec`. Returns
    (ok, error_message, warnings)."""
    result = subprocess.run(
        ["node", str(_CHECKER)],
        input=json.dumps(spec),
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode == 0:
        payload = json.loads(result.stdout)
        return True, "", payload.get("warnings", [])
    try:
        payload = json.loads(result.stderr)
        return False, payload.get("error", result.stderr), []
    except json.JSONDecodeError:
        return False, result.stderr, []


@pytest.mark.skipif(
    shutil.which("node") is None,
    reason="node not on PATH; compile-time spec test requires Node.js",
)
class TestSpecCompilesViaVegaLite:
    """Pass the generated spec through the real Vega-Lite compiler.
    Catches errors like Duplicate signal names that unit tests of
    spec shape can't see.
    """

    def test_aging_spec_compiles_without_errors(self):
        spec = vega_specs.aging_spec(
            _aging_report(
                [
                    _item("#1", "Awaiting Review", 3),
                    _item("#2", "Approved", 50),
                    _item("#3", "Awaiting Review", 100),
                ]
            )
        )
        ok, err, warnings = _compile_via_node(spec)
        assert ok, f"spec failed to compile: {err}"
        # Warnings are tolerated, but a duplicate-signal warning here
        # would mean we've reintroduced the layered-params bug.
        for w in warnings:
            assert "duplicate signal" not in str(w).lower(), (
                f"vega-lite warned about a duplicate signal — the "
                f"params-on-layer fix may have regressed: {w}"
            )

    def test_aging_spec_compiles_with_empty_items(self):
        """No data points but valid spec — must still compile."""
        report = AgingReport(
            input=AgingInput(
                repo="acme/widget",
                asof=date(2026, 5, 14),
                workflow=("A", "B"),
                history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13),
                offline=False,
            ),
            items=[],
            cycle_time_percentiles={50: 1.0, 70: 2.0, 85: 3.0, 95: 5.0},
            completed_count=0,
            interpretation=_interp(),
            generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        spec = vega_specs.aging_spec(report)
        ok, err, _ = _compile_via_node(spec)
        assert ok, f"empty-items spec failed to compile: {err}"

    def test_checker_detects_actual_compile_errors(self):
        """Validate the checker itself: a deliberately broken spec
        (top-level params on a layered spec) must fail to compile with
        an error mentioning duplicate signals. If this test starts
        passing without an error message, the checker is silently
        accepting bad input."""
        broken = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "params": [
                {
                    "name": "zoom",
                    "select": {"type": "interval", "encodings": ["y"]},
                    "bind": "scales",
                }
            ],
            "layer": [
                {
                    "mark": "circle",
                    "data": {"values": [{"x": "A", "y": 1}]},
                    "encoding": {
                        "x": {"field": "x", "type": "nominal"},
                        "y": {"field": "y", "type": "quantitative"},
                    },
                },
                {
                    "mark": "rule",
                    "data": {"values": [{"y": 0.5}]},
                    "encoding": {"y": {"field": "y", "type": "quantitative"}},
                },
            ],
        }
        ok, err, _ = _compile_via_node(broken)
        assert not ok
        assert "duplicate signal" in err.lower(), (
            f"expected duplicate-signal error, got: {err}"
        )
