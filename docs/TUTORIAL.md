# Tutorial — your first dashboard in 5 minutes

This walks you from a clean machine to a working dashboard against a
public GitHub repo. One linear path, copy-paste-able.

For task-specific recipes (scheduling, backup, persistent server,
Docker) see [HOWTO.md](HOWTO.md). For full CLI + YAML + file-layout
detail see [REFERENCE.md](REFERENCE.md).

## 1. Install `uv`

`uv` is a Python toolchain installer. It handles Python itself,
dependencies, and the install of flowmetrics as a global command.

| Platform | One-liner |
|----|----|
| macOS | `brew install uv` |
| Linux | `curl -LsSf https://astral.sh/uv/install.sh \| sh` |
| Windows (PowerShell) | `irm https://astral.sh/uv/install.ps1 \| iex` |

`uv --version` should now print something.

## 2. Install flowmetrics

```bash
uv tool install git+https://github.com/dvhthomas/flowmetrics
```

That puts a `flow` binary on your PATH (`~/.local/bin/flow` on
macOS/Linux). Confirm:

```bash
flow --help
```

> Prefer a source checkout? `git clone` the repo, then
> `uv tool install --from . flowmetrics` from inside it. Either path
> ends with the same global `flow` command.

## 3. Get a GitHub token (one minute)

`flow` reads PRs and issues via the GitHub API, which needs a token.
Easiest path:

```bash
gh auth login       # interactive — pick GitHub.com, HTTPS, browser
```

Or set `GITHUB_TOKEN` directly to a fine-grained PAT with **public
repo read** scope. Public Jira (e.g. Apache) needs no token.

## 4. Write a workflow YAML

A workflow YAML tells `flow` what to fetch. One file per workflow,
in a directory you control. Pick somewhere — say `~/flow/contracts/`.

```bash
mkdir -p ~/flow/contracts ~/flow/data
cat > ~/flow/contracts/astral-uv-week.yaml <<'EOF'
contract:
  name: astral-uv-week
  source: github
  repo: astral-sh/uv
  start: 2026-05-04
  stop:  2026-05-10
EOF
```

That's it — just a contract name, the source, the repo, and the
window you care about. (Jira works the same with `source: jira`,
`jira_url:`, `jira_project:`; label-driven GitHub workflows add a
`wip_labels:` list. See [REFERENCE.md](REFERENCE.md#workflow-yaml).)

## 5. Fetch the data

```bash
flow materialise astral-uv-week \
    --workflows-dir ~/flow/contracts \
    --data-dir       ~/flow/data
```

This hits GitHub (with on-disk caching under `.cache/github/`),
canonicalises the events, and writes Parquet under `~/flow/data/`.
A few seconds on a small window.

## 6. Open the dashboard

```bash
flow serve \
    --workflows-dir ~/flow/contracts \
    --data-dir       ~/flow/data
# → http://127.0.0.1:8000
```

Click around. The dashboard reads only from the local Parquet — no
GitHub during a request — so it stays responsive while you explore.

**What you'll see:**

- **Aging WIP** — every in-flight item by current state × age, pinned
  to the latest data.
- **Cycle Time / Throughput / CFD / Forecast** — driven by the Period
  picker (top of the page).
- **Data source** page (top-right link) — coverage map + a "backfill"
  button that re-runs `flow materialise` for a date range, from the
  browser.

## Next steps

- **Schedule periodic fetches** so the warehouse stays fresh →
  [HOWTO § Schedule data fetches](HOWTO.md#schedule-data-fetches).
- **Run the dashboard as a persistent service** that survives logout
  and reboot → [HOWTO § Run as a persistent web server](HOWTO.md#run-as-a-persistent-web-server).
- **Back up + restore** your warehouse and config →
  [HOWTO § Back up](HOWTO.md#back-up--restore).
- **Ad-hoc reports** for terminals / agents (`flow forecast`,
  `flow efficiency`, …) → [HOWTO § Ad-hoc CLI reports](HOWTO.md#ad-hoc-cli-reports).
