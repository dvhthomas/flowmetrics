"""Generate up-to-date demonstration samples across a curated repo list.

This is the source of truth for the `samples/` directory and the
README's "Sample output" section. Running it:

    uv run python scripts/generate_samples.py

makes live GitHub API calls (one cached pull per repo), writes one
samples-dir per repo with json/text/html for each command, builds a
`samples/index.html` overview, and rewrites the README between
`<!-- BEGIN SAMPLES -->` / `<!-- END SAMPLES -->` markers.

The pure helpers (REPOS, build_index_html, rewrite_readme_samples_section)
are unit-tested. The CLI orchestration is integration-tested by running
the script.
"""

from __future__ import annotations

import html
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
SAMPLES_DIR = PROJECT_ROOT / "samples"
README = PROJECT_ROOT / "README.md"
CACHE_DIR = PROJECT_ROOT / ".cache" / "github"

SAMPLES_BEGIN = "<!-- BEGIN SAMPLES -->"
SAMPLES_END = "<!-- END SAMPLES -->"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Repo:
    slug: str
    archetype: str


REPOS: list[Repo] = [
    Repo("astral-sh/uv", "Fast-moving Rust/Python tooling"),
    Repo("pytest-dev/pytest", "Mature Python framework with active maintenance"),
    Repo("huggingface/transformers", "ML library, mixed community + maintainer flow"),
    Repo("pre-commit/pre-commit", "Developer-tooling Python project"),
    Repo("CalcMark/go-calcmark", "Custom request: Go computational-document tool"),
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


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


def build_index_html(sets: list[SampleSet], generated_at: datetime) -> str:
    rows = []
    for s in sets:
        slug = s.repo.slug
        d = s.efficiency_html.parent.name
        rows.append(f"""
        <tr>
          <td>
            <strong>{html.escape(slug)}</strong><br>
            <span class="archetype">{html.escape(s.repo.archetype)}</span>
          </td>
          <td>
            <a href="{d}/efficiency-week.html">html</a> ·
            <a href="{d}/efficiency-week.txt">text</a> ·
            <a href="{d}/efficiency-week.json">json</a>
          </td>
          <td>
            <a href="{d}/forecast-when-done.html">html</a> ·
            <a href="{d}/forecast-when-done.txt">text</a> ·
            <a href="{d}/forecast-when-done.json">json</a>
          </td>
          <td>
            <a href="{d}/forecast-how-many.html">html</a> ·
            <a href="{d}/forecast-how-many.txt">text</a> ·
            <a href="{d}/forecast-how-many.json">json</a>
          </td>
        </tr>""")

    css = (
        "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        "max-width:1100px;margin:2rem auto;padding:0 1rem;color:#1a1a1a;line-height:1.55;}"
        "h1{font-size:1.6rem;}"
        ".stamp{color:#777;font-size:0.85rem;}"
        "table{border-collapse:collapse;margin:1rem 0;width:100%;}"
        "th,td{padding:0.5rem 0.7rem;border-bottom:1px solid #eee;text-align:left;"
        "vertical-align:top;}"
        "th{background:#fafafa;}"
        ".archetype{color:#777;font-size:0.85rem;}"
        "a{color:#2b7cff;text-decoration:none;}a:hover{text-decoration:underline;}"
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
<p>Live data from public GitHub repositories. Each row gives three
output formats (HTML report, plain text for humans, JSON for agents).</p>
<table>
<thead>
<tr><th>Repository</th><th>Efficiency (week)</th>
<th>Forecast: when-done</th><th>Forecast: how-many</th></tr>
</thead>
<tbody>{"".join(rows)}
</tbody>
</table>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# README rewrite
# ---------------------------------------------------------------------------


def rewrite_readme_samples_section(readme_text: str, new_section: str) -> str:
    if SAMPLES_BEGIN not in readme_text or SAMPLES_END not in readme_text:
        raise ValueError(f"README missing samples markers ({SAMPLES_BEGIN!r} / {SAMPLES_END!r})")
    head, rest = readme_text.split(SAMPLES_BEGIN, 1)
    _, tail = rest.split(SAMPLES_END, 1)
    return f"{head}{SAMPLES_BEGIN}\n{new_section}\n{SAMPLES_END}{tail}"


def build_readme_samples_section(sets: list[SampleSet], generated_at: datetime) -> str:
    lines = [
        f"*Last generated: {generated_at.strftime('%Y-%m-%d %H:%M %Z').strip()}.*",
        "",
        "Five public repos covering a spread of team archetypes. Every link below was",
        "produced by running this tool live against the real GitHub API and is",
        "regenerated every time `uv run python scripts/generate_samples.py` runs.",
        "",
        "| Repo | Archetype | Efficiency | When-done | How-many |",
        "|------|-----------|------------|-----------|----------|",
    ]
    for s in sets:
        d = s.efficiency_html.parent.name
        lines.append(
            f"| `{s.repo.slug}` | {s.repo.archetype} "
            f"| [html](samples/{d}/efficiency-week.html) · "
            f"[text](samples/{d}/efficiency-week.txt) · "
            f"[json](samples/{d}/efficiency-week.json) "
            f"| [html](samples/{d}/forecast-when-done.html) · "
            f"[text](samples/{d}/forecast-when-done.txt) · "
            f"[json](samples/{d}/forecast-when-done.json) "
            f"| [html](samples/{d}/forecast-how-many.html) · "
            f"[text](samples/{d}/forecast-how-many.txt) · "
            f"[json](samples/{d}/forecast-how-many.json) |"
        )
    lines.append("")
    lines.append("Full overview: [samples/index.html](samples/index.html).")
    return "\n".join(lines)


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


def _produce_one_repo(repo: Repo, history_end: str, target_date: str) -> SampleSet:
    """Run all three commands x three formats for one repo."""
    slug_dir = repo.slug.replace("/", "_")
    out_dir = SAMPLES_DIR / slug_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # Window for "efficiency week" — the 7 days ending at history_end (UTC-yesterday).
    end_date = datetime.strptime(history_end, "%Y-%m-%d").replace(tzinfo=UTC).date()
    week_start = (end_date - timedelta(days=6)).isoformat()
    week_stop = end_date.isoformat()

    # Forecast start = today (work begins now); target_date passed in.
    today = datetime.now(UTC).date().isoformat()

    common_efficiency = [
        "efficiency",
        "week",
        "--repo",
        repo.slug,
        "--start",
        week_start,
        "--stop",
        week_stop,
        "--cache-dir",
        str(CACHE_DIR),
    ]
    history_start = (
        datetime.strptime(history_end, "%Y-%m-%d").replace(tzinfo=UTC).date() - timedelta(days=29)
    ).isoformat()
    common_when_done = [
        "forecast",
        "when-done",
        "--repo",
        repo.slug,
        "--items",
        "25",
        "--history-start",
        history_start,
        "--history-end",
        history_end,
        "--start-date",
        today,
        "--runs",
        "10000",
        "--seed",
        "42",
        "--cache-dir",
        str(CACHE_DIR),
    ]
    common_how_many = [
        "forecast",
        "how-many",
        "--repo",
        repo.slug,
        "--target-date",
        target_date,
        "--history-start",
        history_start,
        "--history-end",
        history_end,
        "--start-date",
        today,
        "--runs",
        "10000",
        "--seed",
        "42",
        "--cache-dir",
        str(CACHE_DIR),
    ]

    sets = {}
    for cmd_args, name in [
        (common_efficiency, "efficiency-week"),
        (common_when_done, "forecast-when-done"),
        (common_how_many, "forecast-how-many"),
    ]:
        for fmt, ext in [("text", "txt"), ("json", "json"), ("html", "html")]:
            path = out_dir / f"{name}.{ext}"
            print(f"  {repo.slug} {name} --format {fmt}")
            args = [*cmd_args, "--format", fmt, "--output", str(path)]
            _run_cli(*args)
        sets[name] = name  # placeholder

    return SampleSet(
        repo=repo,
        efficiency_html=out_dir / "efficiency-week.html",
        efficiency_json=out_dir / "efficiency-week.json",
        efficiency_text=out_dir / "efficiency-week.txt",
        when_done_html=out_dir / "forecast-when-done.html",
        when_done_json=out_dir / "forecast-when-done.json",
        when_done_text=out_dir / "forecast-when-done.txt",
        how_many_html=out_dir / "forecast-how-many.html",
        how_many_json=out_dir / "forecast-how-many.json",
        how_many_text=out_dir / "forecast-how-many.txt",
    )


def main() -> None:
    generated_at = datetime.now(UTC)
    history_end = (generated_at.date() - timedelta(days=1)).isoformat()
    target_date = (generated_at.date() + timedelta(days=14)).isoformat()

    print(f"flowmetrics samples — generating at {generated_at.isoformat()}")
    print(f"  training window ends: {history_end}")
    print(f"  forecast target:      {target_date}")

    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    sets: list[SampleSet] = []
    for repo in REPOS:
        print(f"\nRepo: {repo.slug} ({repo.archetype})")
        try:
            sets.append(_produce_one_repo(repo, history_end, target_date))
        except Exception as exc:
            print(f"  SKIP: {exc}")

    if not sets:
        sys.exit("No samples were produced.")

    # Index page
    index_path = SAMPLES_DIR / "index.html"
    index_path.write_text(build_index_html(sets, generated_at), encoding="utf-8")
    print(f"\nWrote {index_path}")

    # README rewrite
    section = build_readme_samples_section(sets, generated_at)
    readme_text = README.read_text(encoding="utf-8")
    README.write_text(rewrite_readme_samples_section(readme_text, section), encoding="utf-8")
    print(f"Updated {README}")


if __name__ == "__main__":
    main()
