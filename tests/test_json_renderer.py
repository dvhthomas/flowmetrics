"""Behavioural spec for the JSON renderer.

Contract:
1. `render(report)` returns valid JSON with trailing newline.
2. Top-level keys: schema, command, generated_at, input, result/training/
   simulation/etc., chart_data, interpretation, logs, docs.
3. Schema matches the report.schema literal.
4. `generated_at` is a valid ISO 8601 string.
5. `logs` is the list passed in (empty default).
6. `chart_data` exists and is non-empty for non-trivial inputs.
7. Dates and timedeltas are JSON-safe (no datetime() in output).
8. render_error(...) produces a structured envelope with schema
   "flowmetrics.error.v1", type, message, hint, command_to_fix, logs, docs.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta

from flowmetrics.compute import FlowEfficiency, WindowResult
from flowmetrics.forecast import build_histogram
from flowmetrics.renderers import json_renderer
from flowmetrics.report import (
    EfficiencyInput,
    EfficiencyReport,
    HowManyInput,
    HowManyReport,
    Interpretation,
    SimulationSummary,
    WhenDoneInput,
    WhenDoneReport,
    build_training_summary,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _interp():
    return Interpretation(headline="head", key_insight="key", next_actions=["a1"], caveats=["c1"])


def _efficiency_report() -> EfficiencyReport:
    pr = FlowEfficiency(
        item_id="#42",
        title="Fix bug",
        created_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
        merged_at=datetime(2026, 5, 5, 17, 0, tzinfo=UTC),
        cycle_time=timedelta(hours=8),
        active_time=timedelta(hours=4),
        efficiency=0.5,
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
            pr_count=1,
            portfolio_efficiency=0.5,
            mean_efficiency=0.5,
            median_efficiency=0.5,
            total_cycle=timedelta(hours=8),
            total_active=timedelta(hours=4),
            per_pr=[pr],
        ),
        interpretation=_interp(),
    )


def _when_done_report() -> WhenDoneReport:
    return WhenDoneReport(
        input=WhenDoneInput(
            "acme/widget",
            50,
            date(2026, 5, 11),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        ),
        training=build_training_summary([3, 5, 0, 7], date(2026, 5, 7), date(2026, 5, 10)),
        simulation=SimulationSummary(runs=1000, seed=42),
        histogram=build_histogram([date(2026, 5, 19), date(2026, 5, 20)]),
        percentiles={
            50: date(2026, 5, 19),
            70: date(2026, 5, 19),
            85: date(2026, 5, 20),
            95: date(2026, 5, 20),
        },
        interpretation=_interp(),
    )


def _how_many_report() -> HowManyReport:
    return HowManyReport(
        input=HowManyInput(
            "acme/widget",
            date(2026, 5, 11),
            date(2026, 5, 25),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        ),
        training=build_training_summary([3, 5, 0, 7], date(2026, 5, 7), date(2026, 5, 10)),
        simulation=SimulationSummary(runs=1000, seed=42),
        histogram=build_histogram([50, 60, 70]),
        percentiles={50: 60, 70: 55, 85: 51, 95: 50},
        interpretation=_interp(),
    )


# ---------------------------------------------------------------------------
# Common shape
# ---------------------------------------------------------------------------


class TestEfficiencyJson:
    def test_returns_valid_json_with_trailing_newline(self):
        out = json_renderer.render(_efficiency_report())
        assert out.endswith("\n")
        json.loads(out)  # raises on invalid

    def test_top_level_keys_present(self):
        payload = json.loads(json_renderer.render(_efficiency_report()))
        for key in [
            "schema",
            "command",
            "generated_at",
            "headline",
            "definition",
            "summary",
            "key_insight",
            "next_actions",
            "caveats",
            "chart_data",
            "input",
            "result",
            "docs",
            "cli_invocation",
            "logs",
        ]:
            assert key in payload, f"missing key: {key}"

    def test_cli_invocation_is_a_reproducible_command(self):
        payload = json.loads(json_renderer.render(_efficiency_report()))
        assert payload["cli_invocation"].startswith("uv run flow efficiency week")
        assert "--repo acme/widget" in payload["cli_invocation"]

    def test_answer_first_field_ordering(self):
        """An agent reading top-down should see the answer before the detail.
        Order: schema → headline → definition → summary → key_insight →
        next_actions → caveats → chart_data → input → ... → logs."""
        out = json_renderer.render(_efficiency_report())
        # Find each key's position in the JSON text
        positions = {
            key: out.index(f'"{key}"')
            for key in [
                "schema",
                "headline",
                "definition",
                "summary",
                "key_insight",
                "next_actions",
                "caveats",
                "chart_data",
                "input",
                "logs",
            ]
        }
        # Answer-first order
        assert positions["schema"] < positions["headline"]
        assert positions["headline"] < positions["definition"]
        assert positions["definition"] < positions["summary"]
        assert positions["summary"] < positions["key_insight"]
        assert positions["key_insight"] < positions["next_actions"]
        assert positions["next_actions"] < positions["caveats"]
        # Detail comes after the answer block
        assert positions["caveats"] < positions["input"]
        assert positions["input"] < positions["logs"]

    def test_headline_and_definition_are_top_level_strings(self):
        payload = json.loads(json_renderer.render(_efficiency_report()))
        # No longer nested under `interpretation` — promoted for agent use
        assert isinstance(payload["headline"], str)
        assert isinstance(payload["definition"], str)
        # Definition explains what's measured
        assert "active" in payload["definition"].lower() or "cycle" in payload["definition"].lower()

    def test_schema_pinned(self):
        payload = json.loads(json_renderer.render(_efficiency_report()))
        assert payload["schema"] == "flowmetrics.efficiency.v1"
        assert payload["command"] == "efficiency week"

    def test_generated_at_is_iso8601(self):
        payload = json.loads(json_renderer.render(_efficiency_report()))
        # Must parse as ISO 8601
        datetime.fromisoformat(payload["generated_at"])

    def test_dates_are_strings(self):
        payload = json.loads(json_renderer.render(_efficiency_report()))
        assert payload["input"]["start"] == "2026-05-04"
        assert payload["input"]["stop"] == "2026-05-10"
        pr = payload["result"]["per_pr"][0]
        assert pr["created_at"] == "2026-05-05T09:00:00+00:00"

    def test_timedeltas_are_encoded_as_object(self):
        payload = json.loads(json_renderer.render(_efficiency_report()))
        td = payload["summary"]["total_cycle"]
        assert "seconds" in td and "hours" in td and "days" in td
        assert td["hours"] == 8.0

    def test_chart_data_has_per_pr_efficiency(self):
        payload = json.loads(json_renderer.render(_efficiency_report()))
        assert "per_pr_efficiency" in payload["chart_data"]
        assert payload["chart_data"]["per_pr_efficiency"][0]["item_id"] == "#42"

    def test_summary_includes_observed_statuses(self):
        """Agents/users tune --active-statuses by inspecting what the
        source actually emits. Surfaced under summary.observed_statuses."""
        from datetime import timedelta as _td

        from flowmetrics.compute import (
            FlowEfficiency as _FE,
        )
        from flowmetrics.compute import (
            WindowResult as _WR,
        )

        per_pr = [
            _FE(
                item_id="BIGTOP-1",
                title="t",
                created_at=datetime(2026, 5, 4, 9, 0, tzinfo=UTC),
                merged_at=datetime(2026, 5, 5, 9, 0, tzinfo=UTC),
                cycle_time=_td(hours=24),
                active_time=_td(0),
                efficiency=0.0,
                statuses_visited=("Open", "Patch Available"),
            ),
        ]
        result = _WR(
            pr_count=1, portfolio_efficiency=0.0,
            mean_efficiency=0.0, median_efficiency=0.0,
            total_cycle=_td(hours=24), total_active=_td(0),
            per_pr=per_pr,
            observed_statuses=["Open", "Patch Available"],
        )
        report = EfficiencyReport(
            input=EfficiencyInput(
                "jira:BIGTOP", date(2026, 5, 4), date(2026, 5, 10),
                4.0, 30.0, False,
                active_statuses=("In Progress",),
            ),
            result=result,
            interpretation=_interp(),
        )
        payload = json.loads(json_renderer.render(report))
        assert payload["summary"]["observed_statuses"] == [
            "Open", "Patch Available",
        ]

    def test_interpretation_promoted_to_top_level(self):
        """Headline + insight + actions are now top-level for agent access."""
        payload = json.loads(json_renderer.render(_efficiency_report()))
        assert payload["headline"] == "head"
        assert payload["next_actions"] == ["a1"]
        assert payload["key_insight"] == "key"
        assert payload["caveats"] == ["c1"]


class TestWhenDoneJson:
    def test_schema(self):
        payload = json.loads(json_renderer.render(_when_done_report()))
        assert payload["schema"] == "flowmetrics.forecast.when_done.v1"
        assert payload["command"] == "forecast when-done"

    def test_percentiles_are_iso_dates_keyed_by_string(self):
        payload = json.loads(json_renderer.render(_when_done_report()))
        assert payload["summary"]["percentiles"]["85"] == "2026-05-20"
        # Reading direction is captured so agents know how to read it
        assert payload["summary"]["reading"].startswith("forward")

    def test_summary_includes_forecast_horizon(self):
        """Surface 'shorter is better': forecast horizon + training-window
        ratio so an agent or human can judge whether to trust the result."""
        payload = json.loads(json_renderer.render(_when_done_report()))
        horizon = payload["summary"]["horizon"]
        # 85th percentile is 2026-05-20; start_date is 2026-05-11 → 9 days
        assert horizon["days_ahead"] == 9
        assert horizon["training_window_days"] == 4  # 4 days in fixture training
        assert horizon["ratio"] == 9 / 4
        assert "shorter is better" in horizon["reading"].lower()

    def test_chart_histogram_has_date_strings(self):
        payload = json.loads(json_renderer.render(_when_done_report()))
        hist = payload["chart_data"]["histogram"]
        assert hist[0]["date"] == "2026-05-19"
        assert hist[0]["frequency"] == 1
        assert payload["chart_data"]["total_runs"] == 2

    def test_training_section_present(self):
        payload = json.loads(json_renderer.render(_when_done_report()))
        assert payload["training"]["total_throughput"] == 15
        # `daily_throughput` is the canonical Vacanti term — not `daily_samples`
        assert payload["training"]["daily_throughput"] == [3, 5, 0, 7]

    def test_vocabulary_block_defines_canonical_terms(self):
        payload = json.loads(json_renderer.render(_when_done_report()))
        vocab = payload["vocabulary"]
        for term in ["Throughput", "Training window", "Monte Carlo Simulation"]:
            assert term in vocab


class TestHowManyJson:
    def test_schema(self):
        payload = json.loads(json_renderer.render(_how_many_report()))
        assert payload["schema"] == "flowmetrics.forecast.how_many.v1"

    def test_percentiles_are_int_keyed_by_string(self):
        payload = json.loads(json_renderer.render(_how_many_report()))
        assert payload["summary"]["percentiles"]["85"] == 51
        assert payload["summary"]["reading"].startswith("backward")

    def test_chart_histogram_has_items(self):
        payload = json.loads(json_renderer.render(_how_many_report()))
        hist = payload["chart_data"]["histogram"]
        assert hist[0]["items"] == 50


# ---------------------------------------------------------------------------
# Logs + errors
# ---------------------------------------------------------------------------


class TestLogs:
    def test_logs_default_to_empty_list(self):
        payload = json.loads(json_renderer.render(_efficiency_report()))
        assert payload["logs"] == []

    def test_logs_passed_through(self):
        payload = json.loads(
            json_renderer.render(_efficiency_report(), logs=["WARNING: stale cache"])
        )
        assert payload["logs"] == ["WARNING: stale cache"]


class TestErrorEnvelope:
    def test_schema(self):
        out = json_renderer.render_error(
            error_type="CacheMiss", message="boom", hint="re-record", logs=["log1"]
        )
        payload = json.loads(out)
        assert payload["schema"] == "flowmetrics.error.v1"
        assert payload["error"]["type"] == "CacheMiss"
        assert payload["error"]["message"] == "boom"
        assert payload["error"]["hint"] == "re-record"
        assert payload["logs"] == ["log1"]
        assert "docs" in payload

    def test_hint_and_command_optional(self):
        payload = json.loads(json_renderer.render_error(error_type="X", message="y"))
        assert "hint" not in payload["error"]
        assert "command_to_fix" not in payload["error"]

    def test_command_to_fix_included_when_provided(self):
        out = json_renderer.render_error(
            error_type="X",
            message="y",
            command_to_fix="uv run flow ...",
        )
        payload = json.loads(out)
        assert payload["error"]["command_to_fix"] == "uv run flow ..."


class TestSchemaShape:
    def test_all_schemas_follow_namespace_pattern(self):
        pattern = re.compile(r"^flowmetrics\.[\w.]+\.v\d+$")
        for r in [_efficiency_report(), _when_done_report(), _how_many_report()]:
            payload = json.loads(json_renderer.render(r))
            assert pattern.match(payload["schema"]), payload["schema"]
        err_payload = json.loads(json_renderer.render_error(error_type="X", message="y"))
        assert pattern.match(err_payload["schema"])
