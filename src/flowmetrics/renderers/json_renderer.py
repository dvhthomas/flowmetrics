"""Agent-consumable JSON output."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Any

from ..report import (
    EfficiencyReport,
    ForecastHorizon,
    HowManyReport,
    Report,
    WhenDoneReport,
    cli_invocation,
    forecast_horizon,
    report_definition,
    report_vocabulary,
)

_DOCS = {
    "metrics": "docs/METRICS.md",
    "forecast": "docs/FORECAST.md",
    "decisions": "docs/DECISIONS.md",
}


def _encode(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _encode(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_encode(x) for x in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return {
            "seconds": obj.total_seconds(),
            "hours": obj.total_seconds() / 3600,
            "days": obj.total_seconds() / 86400,
        }
    return obj


def render(report: Report, *, logs: list[str] | None = None) -> str:
    if isinstance(report, EfficiencyReport):
        payload = _render_efficiency(report)
    elif isinstance(report, WhenDoneReport):
        payload = _render_when_done(report)
    elif isinstance(report, HowManyReport):
        payload = _render_how_many(report)
    else:  # pragma: no cover
        raise TypeError(f"unknown report type: {type(report).__name__}")
    payload["cli_invocation"] = cli_invocation(report)
    payload["logs"] = logs or []
    return json.dumps(payload, indent=2) + "\n"


def render_error(
    *,
    error_type: str,
    message: str,
    hint: str | None = None,
    command_to_fix: str | None = None,
    logs: list[str] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "schema": "flowmetrics.error.v1",
        "error": {"type": error_type, "message": message},
    }
    if hint:
        payload["error"]["hint"] = hint
    if command_to_fix:
        payload["error"]["command_to_fix"] = command_to_fix
    payload["logs"] = logs or []
    payload["docs"] = _DOCS
    return json.dumps(payload, indent=2) + "\n"


# ---------------------------------------------------------------------------
# Per-report
# ---------------------------------------------------------------------------


def _interp(report: Report) -> dict[str, Any]:
    return asdict(report.interpretation)


def _render_efficiency(report: EfficiencyReport) -> dict[str, Any]:
    r = report.result
    return {
        # ── Answer first ────────────────────────────────────────────────
        "schema": report.schema,
        "command": report.command,
        "generated_at": report.generated_at.isoformat(),
        "headline": report.interpretation.headline,
        "definition": report_definition(report),
        "summary": {
            "pr_count": r.pr_count,
            "human_pr_count": r.human_pr_count,
            "bot_pr_count": r.bot_pr_count,
            "portfolio_efficiency": r.portfolio_efficiency,
            "median_efficiency": r.median_efficiency,
            "mean_efficiency": r.mean_efficiency,
            "total_cycle": _encode(r.total_cycle),
            "total_active": _encode(r.total_active),
            "observed_statuses": list(r.observed_statuses),
        },
        "key_insight": report.interpretation.key_insight,
        "next_actions": list(report.interpretation.next_actions),
        "caveats": list(report.interpretation.caveats),
        "vocabulary": report_vocabulary(report),
        # ── Detail ──────────────────────────────────────────────────────
        "chart_data": {
            "per_pr_efficiency": [
                {"item_id": p.item_id, "efficiency": p.efficiency} for p in r.per_pr
            ],
        },
        "input": _encode(asdict(report.input)),
        "result": {
            "per_pr": [
                {
                    "item_id": p.item_id,
                    "title": p.title,
                    "created_at": p.created_at.isoformat(),
                    "merged_at": p.merged_at.isoformat(),
                    "cycle_time": _encode(p.cycle_time),
                    "active_time": _encode(p.active_time),
                    "efficiency": p.efficiency,
                    "is_bot": p.is_bot,
                    "author_login": p.author_login,
                }
                for p in r.per_pr
            ],
        },
        "docs": _DOCS,
    }


def _horizon(h: ForecastHorizon) -> dict[str, Any]:
    return {
        "days_ahead": h.days_ahead,
        "training_window_days": h.training_window_days,
        "ratio": h.ratio,
        "reading": h.reading,
    }


def _render_training(report: WhenDoneReport | HowManyReport) -> dict[str, Any]:
    t = report.training
    return {
        "window_start": t.window_start.isoformat(),
        "window_end": t.window_end.isoformat(),
        "daily_throughput": t.daily_samples,
        "total_throughput": t.total_merges,
        "avg_throughput_per_day": t.avg_per_day,
        "min_throughput_per_day": t.min_per_day,
        "max_throughput_per_day": t.max_per_day,
        "zero_throughput_days": t.zero_days,
    }


def _render_when_done(report: WhenDoneReport) -> dict[str, Any]:
    return {
        # ── Answer first ────────────────────────────────────────────────
        "schema": report.schema,
        "command": report.command,
        "generated_at": report.generated_at.isoformat(),
        "headline": report.interpretation.headline,
        "definition": report_definition(report),
        "summary": {
            "percentiles": {str(p): d.isoformat() for p, d in report.percentiles.items()},
            "reading": "forward — higher confidence means a later date",
            "horizon": _horizon(forecast_horizon(report)),
        },
        "key_insight": report.interpretation.key_insight,
        "next_actions": list(report.interpretation.next_actions),
        "caveats": list(report.interpretation.caveats),
        "vocabulary": report_vocabulary(report),
        # ── Detail ──────────────────────────────────────────────────────
        "chart_data": {
            "histogram": [
                {"date": d.isoformat(), "frequency": report.histogram.counts[d]}
                for d in report.histogram.sorted_keys
            ],
            "total_runs": report.histogram.total,
        },
        "input": _encode(asdict(report.input)),
        "training": _render_training(report),
        "simulation": {"runs": report.simulation.runs, "seed": report.simulation.seed},
        "docs": _DOCS,
    }


def _render_how_many(report: HowManyReport) -> dict[str, Any]:
    return {
        # ── Answer first ────────────────────────────────────────────────
        "schema": report.schema,
        "command": report.command,
        "generated_at": report.generated_at.isoformat(),
        "headline": report.interpretation.headline,
        "definition": report_definition(report),
        "summary": {
            "percentiles": {str(p): n for p, n in report.percentiles.items()},
            "reading": "backward — higher confidence means FEWER items",
            "horizon": _horizon(forecast_horizon(report)),
        },
        "key_insight": report.interpretation.key_insight,
        "next_actions": list(report.interpretation.next_actions),
        "caveats": list(report.interpretation.caveats),
        "vocabulary": report_vocabulary(report),
        # ── Detail ──────────────────────────────────────────────────────
        "chart_data": {
            "histogram": [
                {"items": n, "frequency": report.histogram.counts[n]}
                for n in report.histogram.sorted_keys
            ],
            "total_runs": report.histogram.total,
        },
        "input": _encode(asdict(report.input)),
        "training": _render_training(report),
        "simulation": {"runs": report.simulation.runs, "seed": report.simulation.seed},
        "docs": _DOCS,
    }
