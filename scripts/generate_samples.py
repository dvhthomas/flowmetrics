"""Generate up-to-date demonstration samples across a curated repo list.

This is the source of truth for the `samples/` directory. Running it:

    uv run python scripts/generate_samples.py

makes live GitHub and Jira API calls (cached on disk), writes one
samples-dir per repo with json/text/html for each command, and
rewrites `samples/index.html` — the canonical sample browser
(linked from README + Pages site root).

The pure helpers (REPOS, build_index_html) are unit-tested. The CLI
orchestration is integration-tested by running the script.
"""

from __future__ import annotations

import base64
import html
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
# Output root. Defaults to the tracked `samples/` tree, but
# `FLOWMETRICS_SAMPLES_DIR` redirects it — tests render into a tmp
# dir so a test run never dirties the committed sample gallery.
SAMPLES_DIR = Path(os.environ.get("FLOWMETRICS_SAMPLES_DIR", PROJECT_ROOT / "samples"))
CACHE_DIR = PROJECT_ROOT / ".cache" / "github"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Repo:
    """One sample entry in the demo set.

    `slug` is the display identifier (e.g. ``astral-sh/uv`` or ``ASF/BIGTOP``).
    `cli_args` is the source-selection arguments passed to the CLI;
    everything else (window, runs, seed, format) is added by the script.
    `cache_subdir` keeps GitHub and Jira responses in tidy parallel dirs.

    `cfd_workflow` / `aging_workflow` carry comma-separated workflow
    states (earliest → latest) for the CFD and Aging commands. Left
    None for GitHub: PRs don't expose a named multi-state workflow, so
    we deliberately omit those charts there (see docs/DECISIONS.md #9).
    """

    slug: str
    archetype: str
    cli_args: list[str]
    cache_subdir: str = "github"
    cfd_workflow: str | None = None
    aging_workflow: str | None = None
    # When True, all GitHub commands that support it get `--include-issues`
    # (scatterplot, cfd, forecast when-done/how-many, efficiency, aging).
    # Issues are folded into the pipeline as WorkItems; for Issues closed
    # by a PR-merge, cycle time uses the PR's mergedAt (the causal 'done').
    # GitHub-only.
    stitch_issues: bool = False
    # Per-repo `--gap-hours` for the efficiency command. None = use the
    # CLI default (4h, Vacanti's corporate-synchronous setting). OSS
    # repos with async review cadence typically need a wider gap so
    # cross-day pickup counts as one session — empirically the right
    # knee is at 12–48h depending on the repo's inter-event gap
    # distribution. See docs/TUNING.md.
    efficiency_gap_hours: float | None = None
    # Per-repo `--exclude-stale-days` for CFD + aging. Drops items with
    # no real event activity within N days of the window stop. Useful
    # for repos with large external-contribution backlogs (huggingface,
    # rust-lang/rust) where zombie PRs dominate the headline counts.
    # See docs/TUNING.md.
    exclude_stale_days: int | None = None
    # Per-repo `--wip-labels` for the AGING command only (other commands
    # don't accept it). When set, aging uses the team's named WIP labels
    # as the column order instead of the default Draft → Awaiting Review
    # → Changes Requested → Approved lifecycle. rust-lang/rust uses
    # `S-waiting-on-author,S-waiting-on-review,S-waiting-on-bors` etc.
    aging_wip_labels: str | None = None
    # Per-repo `--active-statuses` for the EFFICIENCY command (Jira
    # only — GitHub falls back to event clustering). Defaults to the
    # CLI's `In Progress,In Development` set which matches generic
    # workflows; rich Jira workflows (Cassandra) need a wider list
    # covering every status the team considers active.
    efficiency_active_statuses: str | None = None


# Default GitHub PR aging workflow — driven by `isDraft` + `reviewDecision`,
# applies to every public repo without configuration. See docs/DECISIONS.md #9.
GITHUB_AGING_WORKFLOW = "Draft,Awaiting Review,Changes Requested,Approved"

# Default GitHub PR CFD workflow — the five-stage PR review lifecycle
# (Draft → Awaiting Review → Changes Requested → Approved → Merged),
# derived from each PR's timeline events by pr_lifecycle_intervals.
# Issues, when included via --include-issues, flow through "Open" before
# joining the PR lifecycle at the closing PR's merge time.
GITHUB_CFD_WORKFLOW = (
    "Draft,Awaiting Review,Changes Requested,Approved,Merged"
)


REPOS: list[Repo] = [
    Repo(
        slug="astral-sh/uv",
        archetype=(
            "Async OSS PR workflow — review cycles span days, not hours. "
            "Uses `--gap-hours=24` because the inter-event gap distribution "
            "(P85=10h, P90=19h) shows cross-day pickup as a single session. "
            "Demonstrates per-repo clustering tuning."
        ),
        cli_args=["--repo", "astral-sh/uv"],
        cfd_workflow=GITHUB_CFD_WORKFLOW,
        aging_workflow=GITHUB_AGING_WORKFLOW,
        efficiency_gap_hours=24.0,
    ),
    Repo(
        slug="pytest-dev/pytest",
        archetype=(
            "Established OSS team with conventional review cadence. "
            "Default `--gap-hours=4` (Vacanti's corporate-synchronous "
            "setting) is appropriate here — contrast with uv's async "
            "rhythm above."
        ),
        cli_args=["--repo", "pytest-dev/pytest"],
        cfd_workflow=GITHUB_CFD_WORKFLOW,
        aging_workflow=GITHUB_AGING_WORKFLOW,
    ),
    Repo(
        slug="huggingface/transformers",
        archetype=(
            "Massive scale with large external-contribution backlog. "
            "Uses `--exclude-stale-days=14` so headline metrics reflect "
            "engaged work, not zombie PRs sitting in queue indefinitely. "
            "Demonstrates signal-vs-noise filtering at OSS scale."
        ),
        cli_args=["--repo", "huggingface/transformers"],
        cfd_workflow=GITHUB_CFD_WORKFLOW,
        aging_workflow=GITHUB_AGING_WORKFLOW,
        exclude_stale_days=14,
    ),
    Repo(
        slug="pre-commit/pre-commit",
        archetype=(
            "Small-team OSS baseline — minimal label vocabulary, default "
            "settings work. The 'no tuning required' anchor for the demo set."
        ),
        cli_args=["--repo", "pre-commit/pre-commit"],
        cfd_workflow=GITHUB_CFD_WORKFLOW,
        aging_workflow=GITHUB_AGING_WORKFLOW,
    ),
    Repo(
        slug="rust-lang/rust",
        archetype=(
            "Rust compiler — large, label-driven OSS workflow with rich "
            "S-* state labels (`S-waiting-on-author`, `S-waiting-on-review`, "
            "etc.). The Aging chart is the standout: columns map directly "
            "to the team's WIP states, so each in-flight PR shows up under "
            "the state it's actually blocked on. Demonstrates label-driven "
            "WIP workflow."
        ),
        cli_args=["--repo", "rust-lang/rust"],
        cfd_workflow=GITHUB_CFD_WORKFLOW,
        # Lowercase: WipLabels.parse normalizes to lower-case (GitHub
        # enforces case-insensitive label uniqueness) and the
        # materialized status_intervals carry lowercase status names.
        # Aging's per-state matching is exact, so --workflow must use
        # the same casing.
        aging_workflow="s-waiting-on-author,s-waiting-on-review,s-waiting-on-bors",
        aging_wip_labels="s-waiting-on-author,s-waiting-on-review,s-waiting-on-bors",
        # Deliberately NO --exclude-stale-days. The whole point of the
        # rust sample is the rich S-state column distribution; filtering
        # aggressively drops the body of PRs that fill the columns.
    ),
    Repo(
        slug="CalcMark/go-calcmark",
        archetype=(
            "Solo developer using Issue+PR linking. Issues capture the work "
            "request, PRs the implementation; `--include-issues` folds both "
            "into the same canonical pipeline and stitched cycle times use "
            "the closing PR's mergedAt. Demonstrates Issue+PR stitching."
        ),
        cli_args=["--repo", "CalcMark/go-calcmark"],
        # Wider aging workflow: Issues land in the leading columns
        # (Open / in-progress) and flow into the PR review lifecycle.
        # CFD also benefits — Issue-driven work shows up before PR
        # creation; PR-only repos still produce identical CFDs because
        # the leading columns simply stay empty.
        cfd_workflow="Open,in-progress,Draft,Awaiting Review,Changes Requested,Approved,Merged",
        aging_workflow="Open,in-progress,Draft,Awaiting Review,Changes Requested,Approved",
        # The Issue+PR stitched demo. This repo's workflow uses an Issue
        # for the work request and a PR for the implementation; with
        # --include-issues every chart command surfaces both populations
        # and uses the PR-merge timestamp for stitched cycle times (so
        # the discussion phase is included, not silently truncated).
        stitch_issues=True,
    ),
    Repo(
        slug="ASF/CASSANDRA",
        archetype=(
            "Apache Cassandra — rich Jira workflow with 5+ explicit statuses. "
            "`status_intervals` come directly from the changelog so efficiency "
            "is computed via status-duration (no event clustering). "
            "Demonstrates Jira-direct workflow versus GitHub-cluster heuristic."
        ),
        cli_args=[
            "--jira-url", "https://issues.apache.org/jira",
            "--jira-project", "CASSANDRA",
        ],
        cache_subdir="jira",
        # Which states count as WIP is a team-level call about what
        # the team has actually committed to working on. For this
        # sample we treat `Triage Needed` and `Open` as wait-for-
        # pickup (so the CFD starts at `In Progress`); a team where
        # triage is itself a tracked, person-assigned activity could
        # legitimately include them. Aging keeps the early states
        # because aging-in-intake is a useful signal regardless.
        cfd_workflow="In Progress,Patch Available,Review In Progress,Ready to Commit,Resolved",
        aging_workflow="Triage Needed,Open,In Progress,Patch Available,Review In Progress,Ready to Commit",
        # Efficiency active set — Cassandra's workflow uses several
        # statuses that all represent "actively being worked":
        # In Progress (development), Patch Available (review queue),
        # Review In Progress (reviewing), Ready to Commit (just before
        # merge). Default `In Progress,In Development` alone misses
        # most items.
        efficiency_active_statuses=(
            "In Progress,Patch Available,Review In Progress,Ready to Commit"
        ),
    ),
    Repo(
        slug="ASF/BIGTOP",
        archetype=(
            "Apache Bigtop — smaller-team Jira project. Same canonical "
            "pipeline as Cassandra; scale variation shows how the same "
            "workflow definitions handle very different team sizes."
        ),
        cli_args=[
            "--jira-url", "https://issues.apache.org/jira",
            "--jira-project", "BIGTOP",
        ],
        cache_subdir="jira",
        # Same convention as the Cassandra sample: `Open` here is
        # treated as wait-for-pickup, so the CFD starts at
        # `In Progress`. Adjust to match whatever the team agrees
        # counts as WIP.
        cfd_workflow="In Progress,Patch Available,Resolved",
        aging_workflow="Open,In Progress,Patch Available",
        # Same wider active set as Cassandra; BIGTOP shares the
        # workflow vocabulary.
        efficiency_active_statuses="In Progress,Patch Available",
    ),
]


@dataclass(frozen=True)
class SampleSet:
    repo: Repo
    # All report paths are `Path | None`. None means the command failed
    # to produce that format — usually a 504 on a heavyweight repo
    # (rust-lang/rust's efficiency + forecast pipelines time out). The
    # index/SAMPLES.md must render `n/a` rather than emit a broken link.
    # CFD/Aging additionally None on repos that don't carry a workflow
    # (GitHub PR-only — see docs/DECISIONS.md #9).
    efficiency_html: Path | None = None
    efficiency_json: Path | None = None
    efficiency_text: Path | None = None
    when_done_html: Path | None = None
    when_done_json: Path | None = None
    when_done_text: Path | None = None
    how_many_html: Path | None = None
    how_many_json: Path | None = None
    how_many_text: Path | None = None
    scatterplot_html: Path | None = None
    scatterplot_json: Path | None = None
    scatterplot_text: Path | None = None
    cfd_html: Path | None = None
    cfd_json: Path | None = None
    cfd_text: Path | None = None
    aging_html: Path | None = None
    aging_json: Path | None = None
    aging_text: Path | None = None


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


# Source markdown linked from the published index. These render on
# github.com (with cross-doc anchors); the Pages site only serves the
# samples directory itself.
REPO_URL = "https://github.com/dvhthomas/flowmetrics"
REFERENCE_DOCS: list[tuple[str, str, str]] = [
    ("README.md", "README", "What flowmetrics is and how to run it."),
    ("docs/METRICS.md", "Metrics", "How cycle / active / wait / flow efficiency are computed."),
    ("docs/TUNING.md", "Tuning", "Per-repo --gap-hours / --exclude-stale-days / --include-issues."),
    ("docs/FORECAST.md", "Forecasting", "Monte Carlo when-done and how-many."),
    ("docs/DECISIONS.md", "Decisions", "Architectural trade-offs and known constraints."),
    ("docs/GLOSSARY.md", "Glossary", "Vacanti terms and our usage."),
]


def _cell(d: str, name: str, present: bool) -> str:
    """One report cell: three format links, or 'n/a' if the report
    wasn't generated for this source."""
    if not present:
        return '<td class="na">n/a</td>'
    return (
        f'<td><a href="{d}/{name}.html">html</a> · '
        f'<a href="{d}/{name}.txt">text</a> · '
        f'<a href="{d}/{name}.json">json</a></td>'
    )


def build_samples_md(sets: list[SampleSet], generated_at: datetime) -> str:
    """Markdown navigation for the samples/ directory.

    Renders well on github.com (so the in-repo file is browsable) and
    in any local Markdown viewer. The corresponding `index.html` is for
    the Pages site; this one is for anyone reading the repo.

    Per-report rows use relative links so the same file works whether
    viewed at `samples/SAMPLES.md` or copied elsewhere in the tree."""
    lines: list[str] = [
        "# Sample reports",
        "",
        f"_Generated {generated_at.strftime('%Y-%m-%d %H:%M:%S %Z').strip()}_",
        "",
        "Open the `.html` files directly in a browser — no server needed; "
        "Vega-Lite loads from CDN via plain `<script>` tags.",
        "",
        "Each report comes in three formats: **html** (interactive chart), "
        "**txt** (terminal output), and **json** (agent-readable envelope). "
        "Reports marked _n/a_ are skipped for sources whose data shape doesn't "
        "support the report (e.g. CFD on a GitHub repo without intermediate "
        "workflow states).",
        "",
    ]

    def _link(d: str, name: str, present: bool) -> str:
        if not present:
            return "_n/a_"
        return (
            f"[html]({d}/{name}.html) · "
            f"[txt]({d}/{name}.txt) · "
            f"[json]({d}/{name}.json)"
        )

    for s in sets:
        slug = s.repo.slug
        d = slug.replace("/", "_")
        lines.append(f"## {slug}")
        lines.append("")
        lines.append(f"_{s.repo.archetype}_")
        lines.append("")
        lines.append("| Report | Formats |")
        lines.append("| --- | --- |")
        lines.append(f"| Efficiency | {_link(d, 'efficiency', s.efficiency_html is not None)} |")
        lines.append(f"| WWIBD: Date | {_link(d, 'forecast-when-done', s.when_done_html is not None)} |")
        lines.append(f"| WWIBD: How Many | {_link(d, 'forecast-how-many', s.how_many_html is not None)} |")
        lines.append(f"| Cycle-time scatterplot | {_link(d, 'scatterplot', s.scatterplot_html is not None)} |")
        lines.append(f"| CFD | {_link(d, 'cfd', s.cfd_html is not None)} |")
        lines.append(f"| Aging WIP | {_link(d, 'aging', s.aging_html is not None)} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## Reference")
    lines.append("")
    for path, label, blurb in REFERENCE_DOCS:
        lines.append(f"- **[{label}]({REPO_URL}/blob/main/{path})** — {blurb}")
    lines.append("")
    return "\n".join(lines)


def build_index_html(sets: list[SampleSet], generated_at: datetime) -> str:
    rows = []
    for s in sets:
        slug = s.repo.slug
        # Derive directory from slug, not from an optional report path —
        # any report can be None when a command timed out or wasn't
        # produced for this source.
        d = slug.replace("/", "_")
        rows.append(
            "\n        <tr>\n"
            f'          <td><strong>{html.escape(slug)}</strong><br>'
            f'<span class="archetype">{html.escape(s.repo.archetype)}</span></td>\n'
            f"          {_cell(d, 'efficiency', s.efficiency_html is not None)}\n"
            f"          {_cell(d, 'forecast-when-done', s.when_done_html is not None)}\n"
            f"          {_cell(d, 'forecast-how-many', s.how_many_html is not None)}\n"
            f"          {_cell(d, 'scatterplot', s.scatterplot_html is not None)}\n"
            f"          {_cell(d, 'cfd', s.cfd_html is not None)}\n"
            f"          {_cell(d, 'aging', s.aging_html is not None)}\n"
            "        </tr>"
        )

    css = (
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:1200px;margin:2rem auto;padding:0 1rem;color:#1a1a1a;line-height:1.55;}"
        "h1{font-size:1.6rem;}"
        "h2{font-size:1.15rem;margin-top:2.5rem;border-bottom:1px solid #eee;"
        "padding-bottom:0.3rem;}"
        ".stamp{color:#777;font-size:0.85rem;}"
        "table{border-collapse:collapse;margin:1rem 0;width:100%;}"
        "th,td{padding:0.5rem 0.7rem;border-bottom:1px solid #eee;text-align:left;"
        "vertical-align:top;font-size:0.9rem;}"
        "th{background:#fafafa;}"
        ".archetype{color:#777;font-size:0.85rem;}"
        ".na{color:#bbb;font-style:italic;}"
        "a{color:#2b7cff;text-decoration:none;}a:hover{text-decoration:underline;}"
        ".note{color:#666;font-size:0.85rem;background:#fafafa;padding:0.6rem 0.8rem;"
        "border-left:3px solid #ddd;margin:0.8rem 0;}"
        "dl dt{margin-top:0.6rem;font-weight:600;}"
        "dl dd{margin:0.1rem 0 0.5rem 1.2rem;color:#555;font-size:0.9rem;}"
    )

    reference_items = "\n".join(
        f'  <dt><a href="{REPO_URL}/blob/main/{path}">{html.escape(label)}</a> '
        f'<span style="color:#888;font-size:0.85rem;">— {html.escape(path)}</span></dt>\n'
        f"  <dd>{html.escape(blurb)}</dd>"
        for path, label, blurb in REFERENCE_DOCS
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>flowmetrics — samples</title>
<style>{css}</style>
</head>
<body>
<h1>flowmetrics — sample output</h1>
<p class="stamp">Generated {generated_at.strftime("%Y-%m-%d %H:%M:%S %Z").strip()}</p>
<p>Live data from public GitHub and Apache Jira sources. Each row gives
three output formats per report (HTML, text, JSON).</p>
<p class="note"><strong>About the GitHub samples:</strong> the
default GitHub CFD uses Open → Merged because PRs don't expose a
multi-state workflow out of the box. Teams that label their PRs
(e.g. <code>S-waiting-on-review</code>, <code>S-waiting-on-author</code>)
can drive a richer multi-band CFD by passing <code>--wip-labels</code>
to <code>flow aging</code>; the same labeling pattern works for CFD
when you pass an ordered <code>--workflow</code>. The GitHub Aging
samples here use the simple review-decision lifecycle
(Draft → Awaiting Review → Changes Requested → Approved); see
<code>docs/DECISIONS.md</code> #9 and #10 for the full reasoning.</p>
<table>
<thead>
<tr><th>Repository</th>
<th>Efficiency (week)</th>
<th>WWIBD: Date</th>
<th>WWIBD: How Many</th>
<th>Scatterplot</th>
<th>CFD</th>
<th>Aging WIP</th></tr>
</thead>
<tbody>{"".join(rows)}
</tbody>
</table>

<h2>Reference</h2>
<p>The samples on this site are produced by the
<a href="{REPO_URL}">flowmetrics</a> CLI. For how the math works, why
each decision was made, and the Vacanti vocabulary used throughout, see
the source documents in the GitHub repo (markdown renders natively
there):</p>
<dl>
{reference_items}
</dl>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Orchestration (live API)
# ---------------------------------------------------------------------------


def _run_cli(*args: str) -> str:
    """Run `uv run flow ...` and return stdout."""
    result = subprocess.run(
        ["uv", "run", "flow", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise RuntimeError(f"flow command failed: flow {' '.join(args)}")
    return result.stdout


def _recover_efficiency_window(out_dir: Path) -> tuple[str, str] | None:
    """When refreshing samples offline, reuse the previous run's
    date window so the cache hits. Day-to-day clock drift (today
    vs yesterday) otherwise produces 100% cache misses after a
    single calendar day.

    Reads the existing `efficiency.json` and returns its
    `(start, stop)` window if available; None otherwise.
    """
    import json as _json
    eff = out_dir / "efficiency.json"
    if not eff.exists():
        return None
    try:
        data = _json.loads(eff.read_text())
        inp = data.get("input") or {}
        start = inp.get("start")
        stop = inp.get("stop")
        if isinstance(start, str) and isinstance(stop, str):
            return start, stop
    except (ValueError, KeyError):
        pass
    return None


def _produce_one_repo(
    repo: Repo,
    history_end: str,
    target_date: str,
    *,
    offline: bool = False,
) -> SampleSet:
    """Run all three commands x three formats for one repo.

    `offline=True` adds `--offline` to every underlying `flow`
    invocation, so cache misses raise instead of fetching live.
    Lets you refresh samples after a spec change without burning
    API quota — and proves the cache covers the configured set.

    When offline AND the repo has a previously-written
    `efficiency.json`, the date window is recovered from there so
    the cache hits. (Without recovery, today's auto-computed window
    drifts one day per calendar day → 100% miss after one night.)
    """
    slug_dir = repo.slug.replace("/", "_")
    out_dir = SAMPLES_DIR / slug_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    recovered = _recover_efficiency_window(out_dir) if offline else None
    if recovered is not None:
        week_start, week_stop = recovered
        end_date = datetime.strptime(week_stop, "%Y-%m-%d").replace(tzinfo=UTC).date()
        history_end = week_stop
    else:
        # Window for the efficiency report — the 7 days ending at history_end (UTC-yesterday).
        end_date = datetime.strptime(history_end, "%Y-%m-%d").replace(tzinfo=UTC).date()
        week_start = (end_date - timedelta(days=6)).isoformat()
        week_stop = end_date.isoformat()

    # Forecast start = today (work begins now); target_date passed in.
    today = datetime.now(UTC).date().isoformat()

    cache_dir = PROJECT_ROOT / ".cache" / repo.cache_subdir
    source_args = list(repo.cli_args)
    common_cache = ["--cache-dir", str(cache_dir)]
    if offline:
        common_cache.append("--offline")
    # GitHub-only; Issues are a GitHub concept. The CLI rejects
    # --include-issues with Jira sources, so guard at script level.
    stitch_args = ["--include-issues"] if repo.stitch_issues else []
    # Per-repo tuning (see docs/TUNING.md):
    #   efficiency_gap_hours: widens the active-time clustering gap so
    #     async OSS PR rhythm reads as one session.
    #   exclude_stale_days: drops items with no activity in N days
    #     from CFD + aging (the noise filter for OSS backlogs).
    gap_args = (
        ["--gap-hours", str(repo.efficiency_gap_hours)]
        if repo.efficiency_gap_hours is not None else []
    )
    stale_args = (
        ["--exclude-stale-days", str(repo.exclude_stale_days)]
        if repo.exclude_stale_days is not None else []
    )

    active_status_args = (
        ["--active-statuses", repo.efficiency_active_statuses]
        if repo.efficiency_active_statuses is not None else []
    )
    common_efficiency = [
        "efficiency",
        *source_args,
        "--start", week_start, "--stop", week_stop,
        *common_cache,
        *stitch_args,
        *gap_args,
        *active_status_args,
    ]
    history_start = (
        datetime.strptime(history_end, "%Y-%m-%d").replace(tzinfo=UTC).date() - timedelta(days=29)
    ).isoformat()
    common_when_done = [
        "forecast", "when-done",
        *source_args,
        "--items", "25",
        "--history-start", history_start, "--history-end", history_end,
        "--start-date", today,
        "--runs", "10000", "--seed", "42",
        *common_cache,
        *stitch_args,
    ]
    common_how_many = [
        "forecast", "how-many",
        *source_args,
        "--target-date", target_date,
        "--history-start", history_start, "--history-end", history_end,
        "--start-date", today,
        "--runs", "10000", "--seed", "42",
        *common_cache,
        *stitch_args,
    ]

    common_scatterplot = [
        "scatterplot",
        *source_args,
        "--start", history_start, "--stop", history_end,
        *common_cache,
        *stitch_args,
    ]
    commands: list[tuple[list[str], str]] = [
        (common_efficiency, "efficiency"),
        (common_when_done, "forecast-when-done"),
        (common_how_many, "forecast-how-many"),
        (common_scatterplot, "scatterplot"),
    ]
    if repo.cfd_workflow is not None:
        # CFD uses the 30-day training window, not the 7-day efficiency
        # window. CFD's shape needs weeks of history to be readable —
        # arrivals/departures look like a tiny ramp inside one week.
        commands.append(
            (
                [
                    "cfd",
                    *source_args,
                    "--start", history_start, "--stop", history_end,
                    "--workflow", repo.cfd_workflow,
                    *common_cache,
                    *stitch_args,
                    *stale_args,
                ],
                "cfd",
            )
        )
    if repo.aging_workflow is not None:
        aging_label_args = (
            ["--wip-labels", repo.aging_wip_labels]
            if repo.aging_wip_labels is not None else []
        )
        commands.append(
            (
                [
                    "aging",
                    *source_args,
                    "--asof", today,
                    "--workflow", repo.aging_workflow,
                    "--history-start", history_start,
                    "--history-end", history_end,
                    *common_cache,
                    *stitch_args,
                    *stale_args,
                    *aging_label_args,
                ],
                "aging",
            )
        )

    # Per-command resilience. Some commands time out on very large
    # repos (rust-lang/rust hits GitHub's 504 on the efficiency fetch).
    # Skipping the failing command lets the rest of the repo's samples
    # still render — important when the failure is a transient
    # gateway timeout, and important when one command is genuinely
    # too heavy (rust CFD) but another (rust aging, which fetches
    # only open PRs) is cheap.
    for cmd_args, name in commands:
        cmd_failed = False
        for fmt, ext in [("text", "txt"), ("json", "json"), ("html", "html")]:
            if cmd_failed:
                break  # Don't retry the other formats of a failed command.
            path = out_dir / f"{name}.{ext}"
            print(f"  {repo.slug} {name} --format {fmt}")
            args = [*cmd_args, "--format", fmt, "--output", str(path)]
            try:
                _run_cli(*args)
            except RuntimeError as exc:
                print(f"    SKIP {repo.slug} {name}: {exc}")
                cmd_failed = True

    def _opt(name: str, ext: str) -> Path | None:
        p = out_dir / f"{name}.{ext}"
        return p if p.exists() else None

    return SampleSet(
        repo=repo,
        efficiency_html=_opt("efficiency", "html"),
        efficiency_json=_opt("efficiency", "json"),
        efficiency_text=_opt("efficiency", "txt"),
        when_done_html=_opt("forecast-when-done", "html"),
        when_done_json=_opt("forecast-when-done", "json"),
        when_done_text=_opt("forecast-when-done", "txt"),
        how_many_html=_opt("forecast-how-many", "html"),
        how_many_json=_opt("forecast-how-many", "json"),
        how_many_text=_opt("forecast-how-many", "txt"),
        scatterplot_html=_opt("scatterplot", "html"),
        scatterplot_json=_opt("scatterplot", "json"),
        scatterplot_text=_opt("scatterplot", "txt"),
        cfd_html=_opt("cfd", "html"),
        cfd_json=_opt("cfd", "json"),
        cfd_text=_opt("cfd", "txt"),
        aging_html=_opt("aging", "html"),
        aging_json=_opt("aging", "json"),
        aging_text=_opt("aging", "txt"),
    )


def main() -> None:
    # Lightweight CLI: just `--offline`. argparse would pull in more
    # surface than this single flag warrants.
    offline = "--offline" in sys.argv[1:]

    generated_at = datetime.now(UTC)
    history_end = (generated_at.date() - timedelta(days=1)).isoformat()
    target_date = (generated_at.date() + timedelta(days=14)).isoformat()

    print(f"flowmetrics samples — generating at {generated_at.isoformat()}")
    print(f"  mode:                 {'offline (cache only)' if offline else 'online (cache + live fetch on miss)'}")
    print(f"  training window ends: {history_end}")
    print(f"  forecast target:      {target_date}")

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    sets: list[SampleSet] = []
    for repo in REPOS:
        print(f"\nRepo: {repo.slug} ({repo.archetype})")
        try:
            sets.append(_produce_one_repo(repo, history_end, target_date, offline=offline))
        except Exception as exc:
            print(f"  SKIP: {exc}")

    if not sets:
        sys.exit("No samples were produced.")

    # Index page — the canonical browse surface for samples. The README
    # links to it but doesn't duplicate the per-repo table.
    index_path = SAMPLES_DIR / "index.html"
    index_path.write_text(build_index_html(sets, generated_at), encoding="utf-8")
    print(f"Wrote {index_path}")

    # Markdown navigation, parallel to index.html — readable on github.com
    # and in local Markdown viewers without rendering the HTML.
    md_path = SAMPLES_DIR / "SAMPLES.md"
    md_path.write_text(build_samples_md(sets, generated_at), encoding="utf-8")
    print(f"Wrote {md_path}")

    # Landing-page preview image (best-effort). Charts are now Vega
    # specs loaded via CDN — there's no embedded PNG to extract. If a
    # stale preview.png is on disk we leave it alone; refreshing it
    # requires a headless-browser screenshot (see
    # scripts/screenshot_sample.sh for the Aging report's preview).
    preview_path = SAMPLES_DIR / "preview.png"
    preview_source = SAMPLES_DIR / "ASF_CASSANDRA" / "cfd.html"
    if preview_source.exists():
        try:
            extract_preview_png(preview_source, preview_path)
            print(f"Wrote {preview_path}")
        except ValueError:
            print(f"(preview.png not regenerated — no embedded PNG in {preview_source.name})")


def extract_preview_png(source_html: Path, dest_png: Path) -> Path:
    """Extract the first base64-embedded PNG from `source_html` and
    write it to `dest_png`. Used to lift a representative chart into a
    standalone file the README can reference inline."""
    text = source_html.read_text(encoding="utf-8")
    match = re.search(r'data:image/png;base64,([A-Za-z0-9+/=]+)', text)
    if not match:
        raise ValueError(f"no base64 PNG found in {source_html}")
    dest_png.parent.mkdir(parents=True, exist_ok=True)
    dest_png.write_bytes(base64.b64decode(match.group(1)))
    return dest_png


if __name__ == "__main__":
    main()
