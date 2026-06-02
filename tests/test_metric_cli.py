"""`flow metric ...` — text+JSON metric extraction for agents.

Replaces the removed top-level chart commands (aging / cfd /
scatterplot — they were chart-primary, the CLI is graphics-free
now). These commands expose the SAME library functions in text +
JSON form so agents and headless humans can reason about the
numbers without rendering anything.

Subcommands:
  throughput  — daily completion counts in a window
  cumulative  — cumulative flow diagram (state counts over time)
  aging       — in-flight items × current state × age
  cycle-time  — completed-item cycle times + P50/P85/P95

Output: text (default, one-line headline) or `--format json`
(versioned envelope). NO HTML, NO charts.
"""
from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from flowmetrics.cli import cli

FIXTURE_CACHE = str(Path(__file__).parent / "fixtures" / "cache")
# The pinned cache covers astral-sh/uv for early-May 2026.
_REPO = "astral-sh/uv"
_START = "2026-05-04"
_STOP = "2026-05-10"


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


class TestMetricGroup:
    def test_metric_group_lists_four_subcommands(self):
        result = _invoke("metric", "--help")
        assert result.exit_code == 0, result.output
        for cmd in ("throughput", "cumulative", "aging", "cycle-time"):
            assert cmd in result.output, f"missing subcommand: {cmd}"


class TestThroughput:
    def test_text_headline_names_repo_and_total(self):
        result = _invoke(
            "metric", "throughput",
            "--repo", _REPO,
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        # The headline names the repo + how many items.
        assert "astral-sh/uv" in result.output
        assert "items" in out or "completed" in out

    def test_json_envelope_carries_per_day_samples(self):
        result = _invoke(
            "metric", "throughput",
            "--repo", _REPO,
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
            "--format", "json",
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.metric.throughput.v1"
        # Per-day sample list aligned with the window length (7 days for
        # the pinned fixture window).
        samples = payload["daily_samples"]
        assert isinstance(samples, list)
        # Inclusive day count: stop − start + 1.
        assert len(samples) == 7
        # Total = sum of samples.
        assert payload["summary"]["total_items"] == sum(samples)


class TestCumulative:
    def test_text_headline_names_workflow_and_end_wip(self):
        result = _invoke(
            "metric", "cumulative",
            "--repo", _REPO,
            "--start", _START, "--stop", _STOP,
            "--workflow", "Open,Merged",
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code == 0, result.output
        assert "astral-sh/uv" in result.output

    def test_json_envelope_carries_per_sample_state_counts(self):
        result = _invoke(
            "metric", "cumulative",
            "--repo", _REPO,
            "--start", _START, "--stop", _STOP,
            "--workflow", "Open,Merged",
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
            "--format", "json",
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.metric.cumulative.v1"
        points = payload["points"]
        assert points
        for pt in points:
            assert "sampled_on" in pt
            assert "counts_by_state" in pt


class TestAging:
    # The pinned fixture cache records `fetch_in_flight(2026-05-10)`
    # + the prior 30-day percentile window. Pin --asof to keep it
    # deterministic and cache-friendly.
    _ASOF = "2026-05-10"

    def test_text_headline_names_in_flight_count(self):
        result = _invoke(
            "metric", "aging",
            "--repo", _REPO,
            "--asof", self._ASOF,
            "--workflow", "Draft,Awaiting Review,Changes Requested,Approved",
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code == 0, result.output
        out = result.output.lower()
        # Headline names how many are in flight (zero is fine — the
        # fixture window may have closed everything).
        assert "in-flight" in out or "in flight" in out or "items" in out

    def test_json_envelope_lists_in_flight_items(self):
        result = _invoke(
            "metric", "aging",
            "--repo", _REPO,
            "--asof", self._ASOF,
            "--workflow", "Draft,Awaiting Review,Changes Requested,Approved",
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
            "--format", "json",
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.metric.aging.v1"
        # Each in-flight row carries id, state, age_days; cycle-time
        # percentiles travel as reference thresholds.
        items = payload["items"]
        assert isinstance(items, list)
        if items:
            for it in items:
                assert "item_id" in it
                assert "current_state" in it
                assert "age_days" in it
        assert "cycle_time_percentiles_days" in payload


class TestCycleTime:
    def test_text_headline_names_percentiles(self):
        result = _invoke(
            "metric", "cycle-time",
            "--repo", _REPO,
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code == 0, result.output
        # Headline carries the P85 reading — that's the canonical
        # commitment threshold.
        assert "P85" in result.output or "p85" in result.output.lower()

    def test_json_envelope_lists_per_item_cycle_times(self):
        result = _invoke(
            "metric", "cycle-time",
            "--repo", _REPO,
            "--start", _START, "--stop", _STOP,
            "--cache-dir", FIXTURE_CACHE,
            "--offline",
            "--format", "json",
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.metric.cycle_time.v1"
        items = payload["items"]
        assert items, "expected at least one completed item"
        for it in items:
            assert "item_id" in it
            assert "completed_at" in it
            assert "cycle_time_days" in it
        percentiles = payload["percentiles_days"]
        # P50 / P70 / P85 / P95.
        for p in (50, 70, 85, 95):
            assert str(p) in percentiles or p in percentiles
