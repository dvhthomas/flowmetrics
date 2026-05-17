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
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SAMPLES_DIR = PROJECT_ROOT / "samples"
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
    # When True, the scatterplot run gets `--include-issues` so closed
    # Issues are plotted alongside merged PRs. For Issues closed by a
    # PR-merge, the cycle time uses the PR's mergedAt (stitched 'done'
    # instant), exposing the Issue-discussion phase that PR-only views
    # invisibly truncate. GitHub-only.
    stitch_issues_in_scatterplot: bool = False


# Default GitHub PR aging workflow — driven by `isDraft` + `reviewDecision`,
# applies to every public repo without configuration. See docs/DECISIONS.md #9.
GITHUB_AGING_WORKFLOW = "Draft,Awaiting Review,Changes Requested,Approved"

# Default GitHub PR CFD workflow — degenerate two-state since PRs don't expose
# a multi-state workflow. The chart looks like arrivals on top, merges on
# bottom; useful as a "what does CFD look like when there's only one band"
# learning reference.
GITHUB_CFD_WORKFLOW = "Open,Merged"


REPOS: list[Repo] = [
    Repo(
        slug="astral-sh/uv",
        archetype="Fast-moving Rust/Python tooling (GitHub)",
        cli_args=["--repo", "astral-sh/uv"],
        cfd_workflow=GITHUB_CFD_WORKFLOW,
        aging_workflow=GITHUB_AGING_WORKFLOW,
    ),
    Repo(
        slug="pytest-dev/pytest",
        archetype="Mature Python framework with active maintenance (GitHub)",
        cli_args=["--repo", "pytest-dev/pytest"],
        cfd_workflow=GITHUB_CFD_WORKFLOW,
        aging_workflow=GITHUB_AGING_WORKFLOW,
    ),
    Repo(
        slug="huggingface/transformers",
        archetype="ML library, mixed community + maintainer flow (GitHub)",
        cli_args=["--repo", "huggingface/transformers"],
        cfd_workflow=GITHUB_CFD_WORKFLOW,
        aging_workflow=GITHUB_AGING_WORKFLOW,
    ),
    Repo(
        slug="pre-commit/pre-commit",
        archetype="Developer-tooling Python project (GitHub)",
        cli_args=["--repo", "pre-commit/pre-commit"],
        cfd_workflow=GITHUB_CFD_WORKFLOW,
        aging_workflow=GITHUB_AGING_WORKFLOW,
    ),
    Repo(
        slug="CalcMark/go-calcmark",
        archetype="Custom request: Go computational-document tool (GitHub)",
        cli_args=["--repo", "CalcMark/go-calcmark"],
        cfd_workflow=GITHUB_CFD_WORKFLOW,
        aging_workflow=GITHUB_AGING_WORKFLOW,
        # The Issue+PR stitched demo. This repo's workflow uses an Issue
        # for the work request and a PR for the implementation; with
        # --include-issues the scatterplot surfaces both populations and
        # uses the PR-merge timestamp for stitched cycle times (so the
        # discussion phase is included, not silently truncated).
        stitch_issues_in_scatterplot=True,
    ),
    Repo(
        slug="ASF/CASSANDRA",
        archetype="Apache Cassandra — active distributed-database project (Jira)",
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
    ),
    Repo(
        slug="ASF/BIGTOP",
        archetype="Apache Bigtop — smaller-team build/packaging project (Jira)",
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
    ),
]


@dataclass(frozen=True)
class SampleSet:
    repo: Repo
    efficiency_html: Path
    efficiency_json: Path
    efficiency_text: Path
    when_done_html: Path
    when_done_json: Path
    when_done_text: Path
    how_many_html: Path
    how_many_json: Path
    how_many_text: Path
    scatterplot_html: Path
    scatterplot_json: Path
    scatterplot_text: Path
    # CFD/Aging are conditional on the repo carrying a workflow.
    # GitHub repos skip CFD (degenerate; see docs/DECISIONS.md #9).
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
        d = s.efficiency_html.parent.name
        lines.append(f"## {slug}")
        lines.append("")
        lines.append(f"_{s.repo.archetype}_")
        lines.append("")
        lines.append("| Report | Formats |")
        lines.append("| --- | --- |")
        lines.append(f"| Efficiency | {_link(d, 'efficiency', True)} |")
        lines.append(f"| WWIBD: Date | {_link(d, 'forecast-when-done', True)} |")
        lines.append(f"| WWIBD: How Many | {_link(d, 'forecast-how-many', True)} |")
        lines.append(f"| Cycle-time scatterplot | {_link(d, 'scatterplot', True)} |")
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
        d = s.efficiency_html.parent.name
        rows.append(
            "\n        <tr>\n"
            f'          <td><strong>{html.escape(slug)}</strong><br>'
            f'<span class="archetype">{html.escape(s.repo.archetype)}</span></td>\n'
            f"          {_cell(d, 'efficiency', True)}\n"
            f"          {_cell(d, 'forecast-when-done', True)}\n"
            f"          {_cell(d, 'forecast-how-many', True)}\n"
            f"          {_cell(d, 'scatterplot', True)}\n"
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

    common_efficiency = [
        "efficiency",
        *source_args,
        "--start", week_start, "--stop", week_stop,
        *common_cache,
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
    ]
    common_how_many = [
        "forecast", "how-many",
        *source_args,
        "--target-date", target_date,
        "--history-start", history_start, "--history-end", history_end,
        "--start-date", today,
        "--runs", "10000", "--seed", "42",
        *common_cache,
    ]

    common_scatterplot = [
        "scatterplot",
        *source_args,
        "--start", history_start, "--stop", history_end,
        *common_cache,
    ]
    if repo.stitch_issues_in_scatterplot:
        common_scatterplot.append("--include-issues")
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
                ],
                "cfd",
            )
        )
    if repo.aging_workflow is not None:
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
                ],
                "aging",
            )
        )

    for cmd_args, name in commands:
        for fmt, ext in [("text", "txt"), ("json", "json"), ("html", "html")]:
            path = out_dir / f"{name}.{ext}"
            print(f"  {repo.slug} {name} --format {fmt}")
            args = [*cmd_args, "--format", fmt, "--output", str(path)]
            _run_cli(*args)

    def _opt(name: str, ext: str) -> Path | None:
        p = out_dir / f"{name}.{ext}"
        return p if p.exists() else None

    return SampleSet(
        repo=repo,
        efficiency_html=out_dir / "efficiency.html",
        efficiency_json=out_dir / "efficiency.json",
        efficiency_text=out_dir / "efficiency.txt",
        when_done_html=out_dir / "forecast-when-done.html",
        when_done_json=out_dir / "forecast-when-done.json",
        when_done_text=out_dir / "forecast-when-done.txt",
        how_many_html=out_dir / "forecast-how-many.html",
        how_many_json=out_dir / "forecast-how-many.json",
        how_many_text=out_dir / "forecast-how-many.txt",
        scatterplot_html=out_dir / "scatterplot.html",
        scatterplot_json=out_dir / "scatterplot.json",
        scatterplot_text=out_dir / "scatterplot.txt",
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
