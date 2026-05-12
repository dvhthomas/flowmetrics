"""Behavioural spec for the CLI --format flag.

Contract:
1. Default format is `text` (human-readable).
2. `--format json` emits structured JSON on stdout. Exit code 0.
3. `--format html` writes a single .html file and prints "Wrote PATH".
4. `--format json` captures stderr-writes (logging, warnings) and
   includes them in the `logs` field of the JSON envelope.
5. Errors in JSON mode emit a `flowmetrics.error.v1` envelope on stdout
   with exit code != 0. The envelope's `logs` field captures stderr too.
6. `--help` for each command mentions agent / JSON output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from flowmetrics.cli import cli

# Absolute path so cwd changes (monkeypatch.chdir) don't break the lookup.
FIXTURE_CACHE = str(Path(__file__).parent / "fixtures" / "cache")


def _invoke(*args: str):
    return CliRunner().invoke(cli, list(args), catch_exceptions=False)


# ---------------------------------------------------------------------------
# Top-level help mentions agent / JSON
# ---------------------------------------------------------------------------


class TestHelpMentionsAgentUsage:
    def test_top_level_help_mentions_json(self):
        result = _invoke("--help")
        assert "json" in result.output.lower()

    def test_efficiency_week_help_mentions_agent_or_json(self):
        result = _invoke("efficiency", "week", "--help")
        assert "agent" in result.output.lower() or "--format json" in result.output

    def test_forecast_when_done_help_mentions_agent_or_json(self):
        result = _invoke("forecast", "when-done", "--help")
        assert "agent" in result.output.lower() or "--format json" in result.output

    def test_forecast_how_many_help_mentions_agent_or_json(self):
        result = _invoke("forecast", "how-many", "--help")
        assert "agent" in result.output.lower() or "--format json" in result.output


# ---------------------------------------------------------------------------
# Default format = text
# ---------------------------------------------------------------------------


class TestDefaultIsText:
    def test_efficiency_week_default_is_text(self):
        result = _invoke(
            "efficiency",
            "week",
            "--repo",
            "astral-sh/uv",
            "--start",
            "2026-05-04",
            "--stop",
            "2026-05-10",
            "--cache-dir",
            FIXTURE_CACHE,
            "--offline",
        )
        assert result.exit_code == 0
        # Text output is not valid JSON
        with pytest.raises(json.JSONDecodeError):
            json.loads(result.output)
        # Has expected human-readable content
        assert "astral-sh/uv" in result.output


# ---------------------------------------------------------------------------
# JSON format
# ---------------------------------------------------------------------------


class TestJsonFormat:
    def test_efficiency_week_json_parses(self):
        result = _invoke(
            "efficiency",
            "week",
            "--repo",
            "astral-sh/uv",
            "--start",
            "2026-05-04",
            "--stop",
            "2026-05-10",
            "--cache-dir",
            FIXTURE_CACHE,
            "--offline",
            "--format",
            "json",
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.efficiency.v1"
        assert "logs" in payload
        assert "headline" in payload
        assert "key_insight" in payload

    def test_forecast_when_done_json_parses(self):
        result = _invoke(
            "forecast",
            "when-done",
            "--repo",
            "astral-sh/uv",
            "--items",
            "25",
            "--history-start",
            "2026-04-11",
            "--history-end",
            "2026-05-10",
            "--start-date",
            "2026-05-11",
            "--runs",
            "100",
            "--seed",
            "42",
            "--cache-dir",
            FIXTURE_CACHE,
            "--offline",
            "--format",
            "json",
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.forecast.when_done.v1"
        assert "summary" in payload
        assert "percentiles" in payload["summary"]
        assert "chart_data" in payload


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------


class TestErrorEnvelope:
    def test_cachemiss_in_json_mode_returns_error_envelope(self, tmp_path):
        # Empty cache dir + --offline → CacheMiss
        result = _invoke(
            "efficiency",
            "week",
            "--repo",
            "astral-sh/uv",
            "--start",
            "2026-05-04",
            "--stop",
            "2026-05-10",
            "--cache-dir",
            str(tmp_path),
            "--offline",
            "--format",
            "json",
        )
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.error.v1"
        assert payload["error"]["type"] == "CacheMiss"
        assert "logs" in payload


# ---------------------------------------------------------------------------
# HTML format writes to file
# ---------------------------------------------------------------------------


class TestStderrEndsUpInJsonLogs:
    """End-to-end: stderr written during the command run must surface in
    JSON.logs so an agent reading stdout-only doesn't miss diagnostics."""

    def test_stderr_print_during_build_appears_in_logs(self, monkeypatch):
        """Patch the service layer to emit a stderr line, then run the
        command in JSON mode and assert that line landed in payload.logs."""
        import sys

        from flowmetrics import service

        original = service.flowmetrics_for_window

        def emit_stderr_then_call(*args, **kwargs):
            print("STDERR_CANARY: cache stale, re-fetching", file=sys.stderr)
            return original(*args, **kwargs)

        monkeypatch.setattr(service, "flowmetrics_for_window", emit_stderr_then_call)
        # cli.py imports service.flowmetrics_for_window into its namespace,
        # so patch the imported reference too.
        from flowmetrics import cli as cli_module

        monkeypatch.setattr(cli_module, "flowmetrics_for_window", emit_stderr_then_call)

        result = _invoke(
            "efficiency",
            "week",
            "--repo",
            "astral-sh/uv",
            "--start",
            "2026-05-04",
            "--stop",
            "2026-05-10",
            "--cache-dir",
            FIXTURE_CACHE,
            "--offline",
            "--format",
            "json",
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert any("STDERR_CANARY" in line for line in payload["logs"]), (
            f"stderr canary not in logs: {payload['logs']}"
        )

    def test_warning_during_build_appears_in_logs(self, monkeypatch):
        import warnings

        from flowmetrics import service

        original = service.flowmetrics_for_window

        def emit_warning_then_call(*args, **kwargs):
            warnings.warn("WARN_CANARY: about to stretch the data", stacklevel=1)
            return original(*args, **kwargs)

        monkeypatch.setattr(service, "flowmetrics_for_window", emit_warning_then_call)
        from flowmetrics import cli as cli_module

        monkeypatch.setattr(cli_module, "flowmetrics_for_window", emit_warning_then_call)

        result = _invoke(
            "efficiency",
            "week",
            "--repo",
            "astral-sh/uv",
            "--start",
            "2026-05-04",
            "--stop",
            "2026-05-10",
            "--cache-dir",
            FIXTURE_CACHE,
            "--offline",
            "--format",
            "json",
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert any("WARN_CANARY" in line for line in payload["logs"])

    def test_stderr_during_error_path_appears_in_error_envelope_logs(self, monkeypatch, tmp_path):
        """Errors must also surface captured stderr in the error envelope."""
        import sys

        def emit_then_raise(*args, **kwargs):
            print("STDERR_DIAG: fetching failed midway", file=sys.stderr)
            raise RuntimeError("simulated failure")

        from flowmetrics import cli as cli_module

        monkeypatch.setattr(cli_module, "flowmetrics_for_window", emit_then_raise)

        result = _invoke(
            "efficiency",
            "week",
            "--repo",
            "astral-sh/uv",
            "--start",
            "2026-05-04",
            "--stop",
            "2026-05-10",
            "--cache-dir",
            str(tmp_path),
            "--offline",
            "--format",
            "json",
        )
        assert result.exit_code != 0
        payload = json.loads(result.output)
        assert payload["schema"] == "flowmetrics.error.v1"
        assert any("STDERR_DIAG" in line for line in payload["logs"])

    def test_no_stderr_means_empty_logs(self):
        """Sanity: when the command runs cleanly with no stderr output,
        logs is an empty list rather than spurious framework chatter."""
        result = _invoke(
            "efficiency",
            "week",
            "--repo",
            "astral-sh/uv",
            "--start",
            "2026-05-04",
            "--stop",
            "2026-05-10",
            "--cache-dir",
            FIXTURE_CACHE,
            "--offline",
            "--format",
            "json",
        )
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["logs"] == []


class TestActiveStatusesFlag:
    """--active-statuses parses correctly and reaches the compute layer
    (verified via --format json, which exposes the input echo)."""

    def test_flag_parses_comma_separated_statuses(self, monkeypatch):
        captured: dict = {}

        from flowmetrics import cli as cli_module

        original = cli_module.flowmetrics_for_window

        def spy(source, start, stop, **kwargs):
            captured["active_statuses"] = kwargs.get("active_statuses")
            return original(source, start, stop, **kwargs)

        monkeypatch.setattr(cli_module, "flowmetrics_for_window", spy)

        result = _invoke(
            "efficiency", "week",
            "--repo", "astral-sh/uv",
            "--start", "2026-05-04", "--stop", "2026-05-10",
            "--cache-dir", FIXTURE_CACHE, "--offline",
            "--active-statuses", "In Progress,Code Review,In Development",
        )
        assert result.exit_code == 0
        assert captured["active_statuses"] == frozenset(
            {"In Progress", "Code Review", "In Development"}
        )

    def test_default_active_statuses_is_in_progress_plus_in_development(self, monkeypatch):
        captured: dict = {}

        from flowmetrics import cli as cli_module

        original = cli_module.flowmetrics_for_window

        def spy(source, start, stop, **kwargs):
            captured["active_statuses"] = kwargs.get("active_statuses")
            return original(source, start, stop, **kwargs)

        monkeypatch.setattr(cli_module, "flowmetrics_for_window", spy)

        result = _invoke(
            "efficiency", "week",
            "--repo", "astral-sh/uv",
            "--start", "2026-05-04", "--stop", "2026-05-10",
            "--cache-dir", FIXTURE_CACHE, "--offline",
        )
        assert result.exit_code == 0
        assert captured["active_statuses"] == frozenset(
            {"In Progress", "In Development"}
        )


class TestHtmlFormat:
    def test_writes_file_to_explicit_output(self, tmp_path):
        out = tmp_path / "report.html"
        result = _invoke(
            "efficiency",
            "week",
            "--repo",
            "astral-sh/uv",
            "--start",
            "2026-05-04",
            "--stop",
            "2026-05-10",
            "--cache-dir",
            FIXTURE_CACHE,
            "--offline",
            "--format",
            "html",
            "--output",
            str(out),
        )
        assert result.exit_code == 0
        assert out.exists()
        content = out.read_text()
        assert "<!doctype html>" in content.lower()
        assert "astral-sh/uv" in content

    def test_default_html_path_under_reports(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        result = _invoke(
            "efficiency",
            "week",
            "--repo",
            "astral-sh/uv",
            "--start",
            "2026-05-04",
            "--stop",
            "2026-05-10",
            "--cache-dir",
            FIXTURE_CACHE,
            "--offline",
            "--format",
            "html",
        )
        assert result.exit_code == 0, result.output
        reports = list((tmp_path / "reports").glob("*.html"))
        assert len(reports) == 1
        assert reports[0].name.startswith("flow-efficiency-week-")
