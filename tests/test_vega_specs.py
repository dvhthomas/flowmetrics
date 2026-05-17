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
from datetime import UTC, date, datetime, timedelta

from flowmetrics.aging import AgingItem
from flowmetrics.compute import FlowEfficiency, WindowResult
from flowmetrics.renderers import vega_specs
from flowmetrics.report import AgingInput, AgingReport, EfficiencyInput, EfficiencyReport


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


def _item(item_id: str, state: str, age: int, url: str | None = None) -> AgingItem:
    return AgingItem(
        item_id=item_id,
        title=f"PR {item_id}",
        current_state=state,
        age_days=age,
        url=url,
    )


class TestAgingDistributionSpec:
    """Horizontal histogram showing count of in-flight items per
    percentile band — replaces the stacked-100% bar that became
    illegible when one band dominated (e.g. 94% past P95 collapses
    the four other bands into tiny slivers)."""

    def _report(self, items_by_age: list[int]) -> AgingReport:
        return _aging_report([
            _item(f"#{i}", "Awaiting Review", age)
            for i, age in enumerate(items_by_age)
        ])

    def test_top_level_shape_is_vega_lite_v5(self):
        spec = vega_specs.aging_distribution_spec(self._report([1, 5, 60]))
        assert spec["$schema"].startswith("https://vega.github.io/schema/vega-lite/")

    def test_one_bar_per_band(self):
        """Five rows of data — one per percentile band — preserved in
        the data values even when a band is empty so the axis stays
        readable."""
        spec = vega_specs.aging_distribution_spec(self._report([1, 5, 60]))
        bar_layer = spec["layer"][0] if "layer" in spec else spec
        values = bar_layer["data"]["values"]
        bands = [v["band"] for v in values]
        assert bands == ["Below P50", "P50–P70", "P70–P85", "P85–P95", "Above P95"]

    def test_band_counts_match_percentile_thresholds(self):
        """P50=1.7, P70=5.4, P85=17.8, P95=57.4 (from _aging_report).
        Items: ages 1, 5, 60 - one in each of Below P50, P50-P70,
        Above P95. The histogram should show those counts."""
        spec = vega_specs.aging_distribution_spec(self._report([1, 5, 60]))
        bar_layer = spec["layer"][0] if "layer" in spec else spec
        values = bar_layer["data"]["values"]
        by_band = {v["band"]: v["count"] for v in values}
        assert by_band["Below P50"] == 1
        assert by_band["P50–P70"] == 1
        assert by_band["P70–P85"] == 0
        assert by_band["P85–P95"] == 0
        assert by_band["Above P95"] == 1

    def test_horizontal_bars_with_band_on_y_count_on_x(self):
        """Y carries the band name (ordinal, sorted by severity),
        X carries the count (quantitative). Axes are labeled."""
        spec = vega_specs.aging_distribution_spec(self._report([1, 5, 60]))
        bar_layer = spec["layer"][0] if "layer" in spec else spec
        encoding = bar_layer["encoding"]
        assert encoding["y"]["field"] == "band"
        assert encoding["x"]["field"] == "count"
        assert encoding["x"]["type"] == "quantitative"
        assert "items" in encoding["x"]["axis"]["title"].lower() or \
               "count" in encoding["x"]["axis"]["title"].lower()

    def test_color_uses_single_sequential_scheme(self):
        """Single color scheme (sequential YlOrRd) — the percentile
        gradient is intrinsic, severity rises with darker shade. Not
        five different unrelated colors."""
        spec = vega_specs.aging_distribution_spec(self._report([1, 5, 60]))
        bar_layer = spec["layer"][0] if "layer" in spec else spec
        color = bar_layer["encoding"]["color"]
        scale = color.get("scale", {})
        # Either a sequential `scheme` or an explicit ordered range
        # that walks one hue family — both satisfy "single scheme".
        assert "scheme" in scale, (
            f"Expected a single sequential color scheme, got {scale}"
        )


class TestAgingChartShadeLayerFieldMatch:
    """The alternating-column shade rect layer uses an `x` encoding
    that has to share a Vega-Lite scale with every other layer's `x`
    encoding (circle, percentile rule labels, per-state counts). All
    those other layers encode `current_state`. If the shade layer
    encodes a DIFFERENT field name (e.g. `state`), Vega-Lite cannot
    unify the x scale across layers and the whole chart fails to
    render — silent at compile time, only manifesting as an error
    in vegaEmbed's `.catch` handler at browser load."""

    def test_shade_layer_uses_current_state_field_name_like_other_layers(self):
        spec = vega_specs.aging_spec(_aging_report([
            _item("#1", "Awaiting Review", 100),
        ]))
        rect_layers = [
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rect"
        ]
        assert rect_layers, "Expected at least one rect (shade) layer."
        for rect in rect_layers:
            assert rect["encoding"]["x"]["field"] == "current_state", (
                f"Shade layer x.field must match the other layers' "
                f"x.field ('current_state'); got "
                f"{rect['encoding']['x'].get('field')!r}"
            )
            values = rect["data"]["values"]
            assert all("current_state" in v for v in values), (
                f"Shade data rows must use 'current_state' as the key, "
                f"got rows {values[:2]}"
            )


class TestAgingSpec:
    def test_top_level_shape_is_vega_lite_v5(self):
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 100)]))
        assert spec["$schema"].startswith("https://vega.github.io/schema/vega-lite/")

    def test_data_values_carry_items_with_required_fields(self):
        items = [
            _item("#1", "Awaiting Review", 3, url="https://x/1"),
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
        assert v1["url"] == "https://x/1"
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
        assert enc["href"]["field"] == "url"

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

    def test_alternating_column_shade_helps_the_eye_locate_stages(self):
        """Every other workflow column gets a very faint background
        tint so the eye can find the right category quickly. The
        shade layer sits behind ALL data and rules so it's an
        unobtrusive zebra-stripe under the chart."""
        workflow = ("A", "B", "C", "D", "E")
        report = AgingReport(
            input=AgingInput(
                repo="acme/widget",
                asof=date(2026, 5, 14),
                workflow=workflow,
                history_start=date(2026, 4, 14),
                history_end=date(2026, 5, 13),
                offline=False,
            ),
            items=[_item("#1", "A", 50)],
            cycle_time_percentiles={50: 1.0, 70: 2.0, 85: 3.0, 95: 5.0},
            completed_count=10,
            interpretation=_interp(),
            generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
        )
        spec = vega_specs.aging_spec(report)
        # First layer should be the shade — paints behind everything.
        first_layer = spec["layer"][0]
        mark = first_layer["mark"]
        mark_type = mark.get("type") if isinstance(mark, dict) else mark
        assert mark_type == "rect"
        # Low opacity — must not dominate.
        assert isinstance(mark, dict) and 0 < mark.get("opacity", 1) <= 0.1
        # Shaded states are every-other in workflow order. Convention:
        # shade the EVEN-indexed columns (0, 2, 4) so the leftmost is
        # gently emphasized; either convention is fine — pin one.
        # Field name must match the other layers' x.field
        # (current_state) so Vega-Lite can unify the x scale.
        shaded_states = {row["current_state"] for row in first_layer["data"]["values"]}
        assert shaded_states == {"A", "C", "E"}

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

    def test_no_red_danger_zone_rect_above_p95(self):
        """Earlier iterations painted the area above P95 with a light-red
        tint encoded by a `y` field. The shade-rect for stage columns
        is different — it encodes `x` only and uses a subtle grey, not
        a y-based red band."""
        spec = vega_specs.aging_spec(_aging_report([_item("#1", "Awaiting Review", 100)]))
        rect_layers = [
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rect"
        ]
        for rect in rect_layers:
            enc = rect.get("encoding", {})
            # Danger-zone rect was identifiable by a `y` encoding on
            # a `y` field — that pattern must be gone.
            assert "y" not in enc, "y-anchored rect (old danger tint) reintroduced"
            mark = rect["mark"]
            if isinstance(mark, dict):
                color = (mark.get("color") or "").lower()
                # No red tint.
                assert not color.startswith("#fee") and not color.startswith("#fdd"), \
                    "red-ish danger color reintroduced on a rect layer"

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


# Note: previous revisions of this file ran each spec through the real
# Vega-Lite compiler via Node + a vendored copy of vega-lite.min.js, to
# catch runtime errors like "Duplicate signal name: zoom_tuple" that
# pure shape-tests miss. With the CDN switch the vendored bundle was
# dropped; the compile-time check is no longer feasible offline. Spec-
# shape tests above still guard the known regression patterns (top-
# level params on layered specs, etc.).


# ---------------------------------------------------------------------------
# Efficiency spec — per-PR FE bars
# ---------------------------------------------------------------------------


def _efficiency_report(per_pr: list[FlowEfficiency]) -> EfficiencyReport:
    from flowmetrics.report import Interpretation
    total_cycle = sum((p.cycle_time for p in per_pr), start=timedelta())
    total_active = sum((p.active_time for p in per_pr), start=timedelta())
    portfolio = (
        total_active.total_seconds() / total_cycle.total_seconds()
        if total_cycle.total_seconds() > 0
        else 0.0
    )
    return EfficiencyReport(
        input=EfficiencyInput(
            repo="acme/widget",
            start=date(2026, 5, 4),
            stop=date(2026, 5, 10),
            gap_hours=4.0,
            min_cluster_minutes=30.0,
            offline=False,
        ),
        result=WindowResult(
            pr_count=len(per_pr),
            portfolio_efficiency=portfolio,
            mean_efficiency=sum(p.efficiency for p in per_pr) / len(per_pr)
            if per_pr else 0.0,
            median_efficiency=sorted(p.efficiency for p in per_pr)[len(per_pr) // 2]
            if per_pr else 0.0,
            total_cycle=total_cycle,
            total_active=total_active,
            per_pr=per_pr,
        ),
        interpretation=Interpretation(
            headline="h", key_insight="k", next_actions=["a"], caveats=["c"]
        ),
        generated_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
    )


def _pr(item_id: str, cycle_hours: float, eff: float, *,
        is_bot: bool = False, title: str = "title") -> FlowEfficiency:
    return FlowEfficiency(
        item_id=item_id,
        title=title,
        created_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC) + timedelta(hours=cycle_hours),
        cycle_time=timedelta(hours=cycle_hours),
        active_time=timedelta(hours=cycle_hours * eff),
        efficiency=eff,
        is_bot=is_bot,
        author_login="alice",
    )


class TestEfficiencySpec:
    """Per-PR FE chart, brought to Aging parity.

    Vacanti framing: long-running PRs dominate the portfolio FE. The
    chart sorts by cycle time descending so the system-bottleneck PRs
    sit at top. Color encodes the FE band (red/yellow/green); the
    portfolio FE is a vertical rule — the system-level reference."""

    def test_top_level_shape_is_vega_lite_v5(self):
        spec = vega_specs.efficiency_spec(_efficiency_report([_pr("#1", 24, 0.4)]))
        assert spec["$schema"].startswith("https://vega.github.io/schema/vega-lite/")

    def test_bar_layer_carries_per_pr_data_with_required_fields(self):
        items = [
            _pr("#1", 24, 0.4, title="Fix bug"),
            _pr("#2", 200, 0.05, title="Slow refactor"),
        ]
        spec = vega_specs.efficiency_spec(_efficiency_report(items))
        bar_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "bar"
        )
        values = bar_layer["data"]["values"]
        assert {v["item_id"] for v in values} == {"#1", "#2"}
        v2 = next(v for v in values if v["item_id"] == "#2")
        assert "title" in v2 and "cycle_hours" in v2 and "efficiency_pct" in v2

    def test_bars_sorted_by_efficiency_ascending(self):
        """Lowest FE at the top, 100% at the bottom — so a 100% bar
        (e.g. a one-shot typo fix that scored a perfect ratio) sits
        BELOW any imperfect bar like the system bottlenecks. The
        earlier sort by cycle-time-descending mostly produced this
        order incidentally, but a long PR that happened to score
        100% would wedge itself between sub-100% bars and break the
        eye's read."""
        spec = vega_specs.efficiency_spec(_efficiency_report([
            _pr("#fast", 1, 1.0),
            _pr("#slow", 200, 0.01),
            _pr("#mid", 50, 0.3),
        ]))
        bar_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "bar"
        )
        y = bar_layer["encoding"]["y"]
        sort = y.get("sort")
        # Must sort by efficiency, ascending. Vega-Lite accepts an
        # object with field + order, a "-field" shorthand, or an
        # explicit value array — accept any encoding that pins the
        # sort key to efficiency_pct.
        if isinstance(sort, dict):
            assert sort.get("field") == "efficiency_pct"
            assert sort.get("order", "ascending") == "ascending"
        elif isinstance(sort, str):
            # "-x" = descending of x; "x" = ascending.
            assert sort == "efficiency_pct" or sort == "x"
        else:
            raise AssertionError(f"unexpected sort encoding: {sort!r}")

    def test_color_encodes_efficiency_band(self):
        """Three FE bands matching the matplotlib chart: red < 10%,
        yellow < 50%, green ≥ 50%. Bots in grey."""
        spec = vega_specs.efficiency_spec(_efficiency_report([_pr("#1", 24, 0.4)]))
        bar_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "bar"
        )
        # color encoding is on a derived band field.
        color = bar_layer["encoding"]["color"]
        # Pinned scheme of three risk-style colors (sequential-or-categorical).
        assert "scale" in color
        domain = color["scale"].get("domain", [])
        # At least the three FE bands plus bot bucket.
        assert any("low" in d.lower() or "red" in d.lower() for d in domain) or \
               any("<" in str(d) for d in domain)

    def test_portfolio_fe_drawn_as_vertical_rule(self):
        """The portfolio FE is the system-level reference, per Vacanti.
        Draw it as a vertical rule line across the bars so the eye can
        see which PRs lie below/above the portfolio number."""
        items = [_pr("#1", 24, 0.4), _pr("#2", 200, 0.05)]
        spec = vega_specs.efficiency_spec(_efficiency_report(items))
        rule_layer = next(
            (layer for layer in spec["layer"]
             if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                 else layer["mark"]) == "rule"),
            None,
        )
        assert rule_layer is not None
        # Its data carries the portfolio_fe percent (not raw ratio).
        values = rule_layer["data"]["values"]
        portfolio_pct = values[0].get("portfolio_pct")
        assert portfolio_pct is not None
        assert 0 <= portfolio_pct <= 100

    def test_bar_tooltip_and_href_channels(self):
        spec = vega_specs.efficiency_spec(_efficiency_report([
            _pr("#42", 24, 0.4, title="Fix bug"),
        ]))
        bar_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "bar"
        )
        enc = bar_layer["encoding"]
        tooltip_fields = {t["field"] for t in enc["tooltip"]}
        assert {"item_id", "title", "efficiency_pct", "cycle_hours"} <= tooltip_fields
        # Click → PR URL.
        assert enc["href"]["field"] == "url"


# ---------------------------------------------------------------------------
# CFD spec — stacked area, the canonical Vacanti chart
# ---------------------------------------------------------------------------


def _cfd_report(points: list, workflow: tuple[str, ...] = ("Open", "In Progress", "Done")):
    from flowmetrics.cfd import CfdPoint
    from flowmetrics.report import CfdInput, CfdReport, Interpretation
    return CfdReport(
        input=CfdInput(
            repo="acme/widget",
            start=date(2026, 5, 1),
            stop=date(2026, 5, 14),
            workflow=workflow,
            interval_days=1,
            offline=False,
        ),
        points=[CfdPoint(d, counts) for d, counts in points],
        interpretation=Interpretation(
            headline="h", key_insight="k", next_actions=["a"], caveats=["c"]
        ),
        generated_at=datetime(2026, 5, 14, 12, 0, tzinfo=UTC),
    )


class TestCfdSpec:
    """Stacked area chart per Vacanti's CFD. Vertical thickness at a
    sample date = WIP in that band (property 3); slope of any line =
    rate at which items entered state-or-later over time (property 5/6).
    """

    def test_top_level_shape_is_vega_lite_v5(self):
        spec = vega_specs.cfd_spec(_cfd_report([
            (date(2026, 5, 1), {"Open": 1, "In Progress": 0, "Done": 0}),
            (date(2026, 5, 14), {"Open": 3, "In Progress": 2, "Done": 5}),
        ]))
        assert spec["$schema"].startswith("https://vega.github.io/schema/vega-lite/")

    def test_area_layer_has_per_state_stacked_values(self):
        spec = vega_specs.cfd_spec(_cfd_report([
            (date(2026, 5, 1), {"Open": 1, "In Progress": 0, "Done": 0}),
            (date(2026, 5, 14), {"Open": 3, "In Progress": 2, "Done": 5}),
        ]))
        area_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "area"
        )
        values = area_layer["data"]["values"]
        # 2 sample dates × 3 states = 6 rows.
        assert len(values) == 6
        # Each row carries the sampled_on date, state name, band width
        # (wip_in_state) and the cumulative line value (for tooltips).
        v0 = values[0]
        assert {"sampled_on", "state", "wip_in_state",
                "entered_at_or_later"} <= v0.keys()

    def test_workflow_state_drives_legend_color_order(self):
        """The color legend lists workflow states in flow order
        (earliest → latest) so the legend reads top-to-bottom in the
        order work moves through the system. (Stack order on the
        chart is the *reverse* of this — terminal state at the visual
        bottom, per Vacanti — but the legend is for navigation, not
        for the chart geometry.)"""
        workflow = ("Open", "In Progress", "Done")
        spec = vega_specs.cfd_spec(_cfd_report([
            (date(2026, 5, 1), {"Open": 1, "In Progress": 0, "Done": 0}),
        ], workflow=workflow))
        area_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "area"
        )
        color = area_layer["encoding"]["color"]
        assert color["sort"] == list(workflow)

    def test_axis_titles_and_x_is_temporal(self):
        spec = vega_specs.cfd_spec(_cfd_report([
            (date(2026, 5, 1), {"Open": 1, "In Progress": 0, "Done": 0}),
        ]))
        area_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "area"
        )
        assert area_layer["encoding"]["x"]["type"] == "temporal"
        assert "Date" in area_layer["encoding"]["x"]["axis"]["title"]
        assert "items" in area_layer["encoding"]["y"]["axis"]["title"].lower() or \
               "WIP" in area_layer["encoding"]["y"]["axis"]["title"] or \
               "Count" in area_layer["encoding"]["y"]["axis"]["title"]

    def test_area_layer_values_are_per_step_band_widths_not_cumulatives(self):
        """The whole CFD bug: feeding cumulative-at-state-or-later
        line values to a `stack: zero` area mark sums them on top of
        each other, inflating the y-axis ~2-3x and making the band
        widths nonsensical.

        The fix: data values are per-step band widths
        (`wip_in_state` = line[step_i] - line[step_{i+1}], or just
        line[terminal] for the bottom band). Stacked, they sum to the
        top line - the correct cumulative-arrivals value."""
        # 3-state workflow with known cumulative line values:
        #   Open=10, In Progress=6, Done=4  ⇒
        #   band[Open] = 10-6 = 4  band[In Progress] = 6-4 = 2  band[Done] = 4
        #   stacked total = 10 = top line. ✓
        spec = vega_specs.cfd_spec(_cfd_report([
            (date(2026, 5, 1), {"Open": 10, "In Progress": 6, "Done": 4}),
        ], workflow=("Open", "In Progress", "Done")))
        area_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "area"
        )
        values = area_layer["data"]["values"]
        by_state = {v["state"]: v for v in values}
        assert by_state["Open"]["wip_in_state"] == 4
        assert by_state["In Progress"]["wip_in_state"] == 2
        assert by_state["Done"]["wip_in_state"] == 4
        # And the area mark's y channel must reference the band-width
        # field, not the cumulative line value.
        assert area_layer["encoding"]["y"]["field"] == "wip_in_state"

    def test_terminal_state_stacks_at_the_visual_bottom(self):
        """Per Vacanti's chart: the terminal workflow step (Done /
        Merged / Resolved) is the bottom band, with the area from
        y=0 up to line[terminal]. The first workflow step (Open /
        Triage Needed) is the top band. With Vega-Lite's
        `stack: "zero"`, the smaller `order` value stacks first (at
        the bottom), so the terminal state must carry the smaller
        order."""
        spec = vega_specs.cfd_spec(_cfd_report([
            (date(2026, 5, 1), {"Open": 10, "In Progress": 6, "Done": 4}),
        ], workflow=("Open", "In Progress", "Done")))
        area_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "area"
        )
        values = area_layer["data"]["values"]
        by_state = {v["state"]: v["stack_order"] for v in values}
        # Terminal (Done) → smallest stack_order; first (Open) → largest.
        assert by_state["Done"] < by_state["In Progress"] < by_state["Open"]

    def test_stacked_band_widths_sum_to_top_line_value(self):
        """Property #1 + #3 invariant: at every sample date, the sum
        of every band width equals the cumulative-arrivals line
        value (= count of the first workflow step). Render correctly
        in Vega-Lite by feeding the band widths and letting
        `stack: "zero"` add them — total height at the date = top
        line. The cumulative-arrivals number is NOT inflated."""
        spec = vega_specs.cfd_spec(_cfd_report([
            (date(2026, 5, 1), {"Open": 10, "In Progress": 6, "Done": 4}),
            (date(2026, 5, 2), {"Open": 15, "In Progress": 10, "Done": 7}),
        ], workflow=("Open", "In Progress", "Done")))
        area_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "area"
        )
        rows = area_layer["data"]["values"]
        by_date: dict[str, list[dict]] = {}
        for v in rows:
            by_date.setdefault(v["sampled_on"], []).append(v)
        # 2026-05-01: bands sum to 10 (= top line "Open")
        assert sum(b["wip_in_state"] for b in by_date["2026-05-01"]) == 10
        # 2026-05-02: bands sum to 15
        assert sum(b["wip_in_state"] for b in by_date["2026-05-02"]) == 15

    def test_hover_tooltip_surfaces_band_width_and_cumulative_per_state(self):
        """Hover tooltip lists every workflow step at the cursor's
        date with BOTH numbers: items currently in that step (the
        band width) AND the cumulative line value (items at-step-
        or-later). Lay readers want the band width; CFD-savvy readers
        want the line value; both are in one place."""
        spec = vega_specs.cfd_spec(_cfd_report([
            (date(2026, 5, 1), {"Open": 10, "In Progress": 6, "Done": 4}),
        ], workflow=("Open", "In Progress", "Done")))
        hover_layer = next(
            layer for layer in spec["layer"]
            if any(
                isinstance(t, dict) and "pivot" in t
                for t in layer.get("transform", [])
            )
        )
        tooltip_fields = {t["field"]: t.get("title", t["field"])
                          for t in hover_layer["encoding"]["tooltip"]
                          if "field" in t}
        # Every state appears as a band-width field…
        for state in ("Open", "In Progress", "Done"):
            assert state in tooltip_fields, (
                f"Tooltip must include the {state!r} band width; "
                f"got fields {list(tooltip_fields)}"
            )
        # And total WIP is computed as top-line − bottom-line.
        assert "wip" in tooltip_fields, (
            f"Tooltip must include the WIP-in-flight summary field; "
            f"got fields {list(tooltip_fields)}"
        )

    def test_hover_layer_shows_all_state_counts_for_the_hovered_date(self):
        """Hovering on the CFD must surface every workflow state's
        cumulative count at the hovered date in a single tooltip — not
        just the count for the band under the cursor. Implementation:
        a separate hover layer (rule or point) carrying a wide-format
        pivot transform so its tooltip can list one row per state plus
        the WIP gap."""
        workflow = ("Open", "In Progress", "Done")
        spec = vega_specs.cfd_spec(_cfd_report(
            [(date(2026, 5, 1), {"Open": 5, "In Progress": 2, "Done": 1}),
             (date(2026, 5, 8), {"Open": 9, "In Progress": 3, "Done": 4})],
            workflow=workflow,
        ))
        hover_layers = [
            layer for layer in spec["layer"]
            if any(
                isinstance(t, dict) and "pivot" in t
                for t in layer.get("transform", [])
            )
        ]
        assert hover_layers, (
            "Expected a hover layer with a pivot transform that widens "
            "the long-format data so every state appears in one tooltip."
        )
        hover = hover_layers[0]
        # The pivot widens the per-state band widths so each workflow
        # state becomes a column carrying its `wip_in_state` value.
        pivot = next(t for t in hover["transform"] if "pivot" in t)
        assert pivot["pivot"] == "state"
        assert pivot["value"] == "wip_in_state"
        # The tooltip must reference each state name as a field.
        tooltip = hover["encoding"]["tooltip"]
        tooltip_fields = {t["field"] for t in tooltip if "field" in t}
        for state in workflow:
            assert state in tooltip_fields, (
                f"Multi-row tooltip must include state {state!r}; "
                f"got fields {tooltip_fields}"
            )
        # And the WIP gap (top minus bottom) belongs in the tooltip too —
        # the actionable signal the prose banner used to carry.
        assert any(
            "WIP" in (t.get("title", "") or "")
            for t in tooltip
        ), "Tooltip must include a 'WIP' row (top-state minus bottom-state)."

    def test_area_uses_linear_interpolation_one_inflection_per_sample(self):
        """Each sample point is a single inflection on the line, not
        a flat-column step. Matches Vacanti's reference shape and
        makes the hover-rule / x-axis-tick alignment unambiguous —
        a sample at date T is a vertex at T's exact timestamp, and
        the tick for T sits at the same timestamp."""
        spec = vega_specs.cfd_spec(_cfd_report([
            (date(2026, 5, 1), {"Open": 5, "Done": 0}),
            (date(2026, 5, 2), {"Open": 6, "Done": 1}),
        ], workflow=("Open", "Done")))
        area_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "area"
        )
        assert area_layer["mark"]["interpolate"] == "linear"

    def test_x_axis_forces_daily_tick_count(self):
        """For a daily-sampled CFD over a ~30-day window, Vega's
        auto-thinning shows labels every other day. That makes the
        chart feel weekly even though every column is one day. Force
        a tick per sample so the granularity is visually obvious; let
        Vega thin LABELS automatically if they crowd, but ticks
        themselves stay at every sample."""
        spec = vega_specs.cfd_spec(_cfd_report([
            (date(2026, 5, 1), {"Open": 5, "Done": 0}),
            (date(2026, 5, 2), {"Open": 6, "Done": 0}),
            (date(2026, 5, 3), {"Open": 7, "Done": 1}),
        ], workflow=("Open", "Done")))
        area_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "area"
        )
        x_axis = area_layer["encoding"]["x"]["axis"]
        # `tickCount` set to a day-granular value drives Vega to emit
        # one tick per day rather than auto-thinning to ~5 ticks.
        assert "tickCount" in x_axis

    def test_no_corner_text_annotation_on_the_chart(self):
        """The earlier 'WIP X → Y ▲' label sat in the chart's top-
        right corner. It was visually weak (small green text on a
        coloured background), didn't align with the WIP signal the
        reader was looking at (the vertical gap at the right edge of
        the area chart), and the same data already lives in the
        hover tooltip — keep the chart minimal and let the tooltip
        carry the per-date numbers."""
        spec = vega_specs.cfd_spec(_cfd_report(
            [(date(2026, 5, 1), {"Open": 5, "Done": 0}),
             (date(2026, 5, 14), {"Open": 10, "Done": 10})],
            workflow=("Open", "Done"),
        ))
        text_layers = [
            layer for layer in spec["layer"]
            if (layer["mark"]["type"] if isinstance(layer["mark"], dict)
                else layer["mark"]) == "text"
        ]
        assert text_layers == [], (
            f"Expected zero text-mark annotations on the chart; got {text_layers}"
        )

    def test_hover_rule_is_legible_when_active(self):
        """The hover rule snaps to the nearest sample date but at
        strokeWidth 1 with a mid-grey colour it reads as a faint
        dashed line, hard to associate with a specific x-axis label.
        Pin it to ≥ 2 px wide and use a darker hue so the eye finds
        which date the tooltip is reading from."""
        spec = vega_specs.cfd_spec(_cfd_report(
            [(date(2026, 5, 1), {"Open": 5, "Done": 0})],
            workflow=("Open", "Done"),
        ))
        hover = next(
            layer for layer in spec["layer"]
            if any(
                isinstance(t, dict) and "pivot" in t
                for t in layer.get("transform", [])
            )
        )
        mark = hover["mark"]
        assert mark["strokeWidth"] >= 2, (
            f"Hover rule strokeWidth must be ≥ 2 px for legibility; "
            f"got {mark.get('strokeWidth')}"
        )

    def test_hover_wip_calculate_is_valid_javascript(self):
        """Vega compiles `calculate` strings via `new Function()`, so
        any expression we emit must be valid JS. The hover layer's
        `wip` calc sums the non-terminal band widths via field access
        on the pivoted columns — single-quoted state names embedded
        in a JS string mean we have to be careful that quoting,
        spacing, and operator precedence all hold.

        Regression guard: an earlier `state_index` calc was emitted as
        `A ? 0 || B ? 1 : -1` (missing the `:` after A) and Vega's
        promise rejected with 'Unexpected end of input' at render
        time."""
        import subprocess

        spec = vega_specs.cfd_spec(_cfd_report(
            [(date(2026, 5, 1), {"Open": 1, "In Progress": 0, "Done": 0})],
            workflow=("Open", "In Progress", "Done"),
        ))
        hover_layer = next(
            layer for layer in spec["layer"]
            if any(
                isinstance(t, dict) and "pivot" in t
                for t in layer.get("transform", [])
            )
        )
        calc = next(
            t["calculate"] for t in hover_layer["transform"]
            if t.get("as") == "wip"
        )
        # Compile through node to validate JS syntax — this is what
        # Vega does internally via new Function() when it evaluates the
        # expression at runtime. Pass the expression via JSON.parse on
        # a stdin-fed payload so single quotes inside the expression
        # don't fight the shell.
        result = subprocess.run(
            ["node", "-e",
             "let s=''; "
             "process.stdin.on('data', c => s += c); "
             "process.stdin.on('end', () => { "
             "  const expr = JSON.parse(s); "
             "  new Function('datum', 'return (' + expr + ')'); "
             "});"],
            input=json.dumps(calc),
            capture_output=True, text=True,
        )
        assert result.returncode == 0, (
            f"state_index calculate expression is not valid JS: "
            f"{calc!r}\nstderr: {result.stderr}"
        )

    def test_two_state_github_degenerate_case_still_renders(self):
        """GitHub PR CFD is two-state (Open, Merged) per DECISIONS.md.
        The chart should render — degenerate but valid — not skip."""
        spec = vega_specs.cfd_spec(_cfd_report(
            [(date(2026, 5, 1), {"Open": 1, "Merged": 0}),
             (date(2026, 5, 14), {"Open": 3, "Merged": 7})],
            workflow=("Open", "Merged"),
        ))
        area_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "area"
        )
        states = {v["state"] for v in area_layer["data"]["values"]}
        assert states == {"Open", "Merged"}


# ---------------------------------------------------------------------------
# Forecast specs — when-done (date histogram) + how-many (count histogram)
# ---------------------------------------------------------------------------


def _when_done_fixture():
    from flowmetrics.forecast import build_histogram
    from flowmetrics.report import (
        Interpretation,
        SimulationSummary,
        TrainingSummary,
        WhenDoneInput,
        WhenDoneReport,
    )
    return WhenDoneReport(
        input=WhenDoneInput(
            repo="acme/widget", items=30,
            start_date=date(2026, 5, 12),
            history_start=date(2026, 4, 12),
            history_end=date(2026, 5, 11),
            offline=False,
        ),
        training=TrainingSummary(
            window_start=date(2026, 4, 12), window_end=date(2026, 5, 11),
            daily_samples=[2, 1, 3, 2, 4, 0, 0] * 4 + [1, 0],
            total_merges=46, avg_per_day=1.5,
            min_per_day=0, max_per_day=4, zero_days=10,
        ),
        simulation=SimulationSummary(runs=10000, seed=42),
        histogram=build_histogram(
            [date(2026, 5, 19)] * 100
            + [date(2026, 5, 20)] * 250
            + [date(2026, 5, 21)] * 350
            + [date(2026, 5, 22)] * 200
            + [date(2026, 5, 23)] * 80
            + [date(2026, 5, 24)] * 20
        ),
        percentiles={
            50: date(2026, 5, 21),
            70: date(2026, 5, 22),
            85: date(2026, 5, 22),
            95: date(2026, 5, 23),
        },
        interpretation=Interpretation(
            headline="h", key_insight="k", next_actions=["a"], caveats=["c"]
        ),
        generated_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
    )


def _how_many_fixture():
    from flowmetrics.forecast import build_histogram
    from flowmetrics.report import (
        HowManyInput,
        HowManyReport,
        Interpretation,
        SimulationSummary,
        TrainingSummary,
    )
    return HowManyReport(
        input=HowManyInput(
            repo="acme/widget",
            start_date=date(2026, 5, 12),
            target_date=date(2026, 5, 26),
            history_start=date(2026, 4, 12),
            history_end=date(2026, 5, 11),
            offline=False,
        ),
        training=TrainingSummary(
            window_start=date(2026, 4, 12), window_end=date(2026, 5, 11),
            daily_samples=[2, 1, 3, 2, 4, 0, 0] * 4 + [1, 0],
            total_merges=46, avg_per_day=1.5,
            min_per_day=0, max_per_day=4, zero_days=10,
        ),
        simulation=SimulationSummary(runs=10000, seed=42),
        histogram=build_histogram([15] * 50 + [20] * 200 + [25] * 400 + [30] * 250 + [35] * 100),
        percentiles={50: 28, 70: 25, 85: 22, 95: 18},
        interpretation=Interpretation(
            headline="h", key_insight="k", next_actions=["a"], caveats=["c"]
        ),
        generated_at=datetime(2026, 5, 12, 14, 30, tzinfo=UTC),
    )


class TestWhenDoneSpec:
    """Vertical bars over completion dates. Percentile dates marked as
    vertical rules — P85 solid + heavier (the forecast threshold per
    Vacanti's recommendation); others dashed."""

    def test_top_level_shape_is_vega_lite_v5(self):
        spec = vega_specs.when_done_spec(_when_done_fixture())
        assert spec["$schema"].startswith("https://vega.github.io/schema/vega-lite/")

    def test_bar_layer_carries_histogram_rows(self):
        spec = vega_specs.when_done_spec(_when_done_fixture())
        bar_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "bar"
        )
        # Each row = one completion date + frequency.
        v0 = bar_layer["data"]["values"][0]
        assert "outcome" in v0 and "frequency" in v0
        assert bar_layer["encoding"]["x"]["type"] == "temporal"

    def test_percentile_rule_lines_one_per_pct(self):
        spec = vega_specs.when_done_spec(_when_done_fixture())
        rule_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rule"
        )
        rows = rule_layer["data"]["values"]
        pcts = {row["pct"] for row in rows}
        assert pcts == {"P50", "P70", "P85", "P95"}

    def test_p85_rule_is_solid_and_heavier(self):
        """Same convention as Aging: P85 stands out as the forecast
        threshold; others are dashed and lighter."""
        spec = vega_specs.when_done_spec(_when_done_fixture())
        rule_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rule"
        )
        enc = rule_layer["encoding"]
        assert "strokeDash" in enc and "condition" in enc["strokeDash"]
        assert "size" in enc and "condition" in enc["size"]


class TestHowManySpec:
    """Same shape as when-done but X-axis is item count, not date.
    Read BACKWARD: higher confidence ⇒ FEWER items."""

    def test_top_level_shape_is_vega_lite_v5(self):
        spec = vega_specs.how_many_spec(_how_many_fixture())
        assert spec["$schema"].startswith("https://vega.github.io/schema/vega-lite/")

    def test_bar_x_is_quantitative_item_count(self):
        spec = vega_specs.how_many_spec(_how_many_fixture())
        bar_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "bar"
        )
        assert bar_layer["encoding"]["x"]["type"] == "quantitative"
        v0 = bar_layer["data"]["values"][0]
        assert isinstance(v0["outcome"], int)

    def test_percentile_rule_lines_at_item_counts(self):
        spec = vega_specs.how_many_spec(_how_many_fixture())
        rule_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "rule"
        )
        ys = {row["x"] for row in rule_layer["data"]["values"]}
        assert ys == {18, 22, 25, 28}

    def test_bars_have_substantial_width_for_quantitative_x(self):
        """With a quantitative x and a wide range (e.g. 0-42 items)
        Vega-Lite's default bar width is a thin line — most of the
        chart becomes whitespace and the distribution shape is hard
        to read. Force a chunkier minimum so each bar fills its
        column's slot."""
        spec = vega_specs.how_many_spec(_how_many_fixture())
        bar_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "bar"
        )
        mark = bar_layer["mark"]
        # Either explicit width via `mark.size` OR a width binding that
        # widens automatically (e.g. `binSpacing: 0` + `bin`); we use
        # the explicit-size pattern.
        assert "size" in mark or "width" in mark, (
            f"Forecast bars need an explicit minimum width to be "
            f"readable; got mark={mark}"
        )


# ---------------------------------------------------------------------------
# Scatterplot spec
# ---------------------------------------------------------------------------


def _scatterplot_fixture():
    from flowmetrics.report import (
        Interpretation,
        ScatterplotInput,
        ScatterplotPoint,
        ScatterplotReport,
    )
    return ScatterplotReport(
        input=ScatterplotInput(
            repo="acme/widget",
            start=date(2026, 4, 15),
            stop=date(2026, 5, 14),
            offline=False,
        ),
        points=[
            ScatterplotPoint(
                item_id="#1", title="alpha",
                completed_at=date(2026, 4, 20),
                cycle_time_days=3.5, url="https://x/1",
            ),
            ScatterplotPoint(
                item_id="#2", title="beta",
                completed_at=date(2026, 5, 1),
                cycle_time_days=10.0, url=None,
            ),
        ],
        cycle_time_percentiles={50: 5.0, 70: 7.0, 85: 9.0, 95: 12.0},
        interpretation=Interpretation(
            headline="h", key_insight="k", next_actions=["a"], caveats=["c"],
        ),
    )


class TestScatterplotSpec:
    """Vacanti's Cycle-Time Scatterplot: x = completion date,
    y = cycle time, dots = completed items, horizontal percentile
    lines (P50/P70/P85/P95)."""

    def test_tooltip_date_field_does_not_rely_on_formatType_utc(self):
        """Regression guard for the NaN-on-hover bug: when the tooltip
        configures a temporal field with `formatType: "utc"` AND the
        data row's date is an ISO string parsed back through Vega's
        date formatter, the browser renders "NaN". Same root cause as
        the CFD tooltip bug (a205d5e). Fix: pre-format the date in
        Python as a nominal string field.

        Concretely: the tooltip's completion-date entry must be a
        nominal field reading a pre-formatted string (e.g.
        `completed_label`), NOT a `temporal`+`formatType:"utc"` combo
        on the raw ISO `completed_at`."""
        spec = vega_specs.scatterplot_spec(_scatterplot_fixture())
        circle_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "circle"
        )
        date_tooltip = next(
            (t for t in circle_layer["encoding"]["tooltip"]
             if t.get("title", "").lower() in {"completed", "completion date"}),
            None,
        )
        assert date_tooltip is not None, "Tooltip must include the completion date"
        # The fragile combo we don't want:
        assert date_tooltip.get("formatType") != "utc", (
            f"Tooltip date field must not use formatType:'utc' on a "
            f"temporal field — it renders as NaN in the browser. "
            f"Use a pre-formatted nominal string field. Got "
            f"{date_tooltip}"
        )
        # And the date field a row carries SHOULD have a pre-formatted
        # label available for nominal display.
        row = circle_layer["data"]["values"][0]
        assert any(
            isinstance(v, str) and "2026" in v
            for k, v in row.items()
            if k.endswith("_label") or k.endswith("_display")
        ), (
            f"Each data row must carry a pre-formatted, human-readable "
            f"date string for the tooltip; got row keys {list(row.keys())}"
        )

    def test_scatterplot_supports_drag_zoom_on_both_axes(self):
        """Cycle-time data has a wide dynamic range (a deep-tail
        item at 3999 days flattens everything else into a thin
        bottom strip). A drag-to-zoom selection bound to the scales
        lets the reader investigate clusters without re-running the
        report. Vega-Lite's interval-selection-with-bind:scales
        gives drag + scroll-zoom on both X (date) and Y (cycle time)
        for free."""
        spec = vega_specs.scatterplot_spec(_scatterplot_fixture())
        circle_layer = next(
            layer for layer in spec["layer"]
            if (layer["mark"].get("type") if isinstance(layer["mark"], dict)
                else layer["mark"]) == "circle"
        )
        params = circle_layer.get("params", [])
        zoom = next(
            (p for p in params if p.get("select", {}).get("type") == "interval"),
            None,
        )
        assert zoom is not None, (
            f"Circle layer must declare an interval-selection param "
            f"for drag-zoom. Got params={params}"
        )
        assert zoom.get("bind") == "scales", (
            f"Interval selection must bind to scales so drag-zoom "
            f"updates the chart axes. Got bind={zoom.get('bind')!r}"
        )
        encodings = zoom["select"].get("encodings", [])
        assert "x" in encodings and "y" in encodings, (
            f"Zoom must cover both X and Y axes — cycle time and date "
            f"both compress badly without per-axis zoom. Got encodings="
            f"{encodings}"
        )
