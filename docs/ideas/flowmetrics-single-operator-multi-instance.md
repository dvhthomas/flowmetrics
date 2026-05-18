# flowmetrics: single-operator, multi-instance warehouse + web UI + MCP

> One-pager produced via `/agent-skills:idea-refine` on 2026-05-18.
> Source of truth for the direction; the feature spec
> (`docs/SPEC-warehouse-app.md`) is being rewritten to match this.

---

## Problem Statement

*How might we get fast, sliceable, render-perfect flow-metrics
dashboards plus an MCP server for agents — for a single operator
running one or more instances across personal + work contexts,
accessible over Tailscale/Caddy, without multi-user platform
overhead?*

---

## Recommended Direction

**Single-operator, multi-instance, dual-surface.** Build the warehouse
(Parquet + DuckDB), a single-page web dashboard (FastAPI + HTMX +
Vega-Lite), and an MCP server. ETL is a separate
`flow materialise <contract>` CLI command driven by external cron /
systemd-timer / k8s-cronjob — never an in-process scheduler. The
runtime process serves the dashboard and the MCP server; it never
fetches from GitHub/Jira directly.

The architecture supports **multiple instances of the same binary**,
each with its own data directory and contracts. One for personal
projects, one for each work team. Tailscale or Caddy in front lets the
user view from any device. Network exposure forces a small but real
auth surface — a shared password for non-localhost binds — but not
OIDC, not roles, not multi-user.

The dashboard is **one page** with anchored sections (`#cycle-time`,
`#aging`, `#forecast`, `#cfd`) so it's scrollable and deep-linkable.
Charts are the most important thing; they get the visual weight. MCP
exposes the same data as typed tools so the user can ask Claude (or
any MCP client) the same questions from a terminal or chat.

---

## Key Assumptions to Validate

- [ ] **Sliced dashboards beat static reports for daily/weekly use.**
  Test: dogfood Slice 3 (window + filter) for 2 weeks.
- [ ] **MCP-via-Claude is useful for flow questions.** Test: in
  Slice 7, ship one MCP tool against Claude Desktop, ask one real
  question.
- [ ] **Multi-instance via separate processes + data dirs is simpler
  than multi-tenancy.** Test: Slice 8 — stand up a second instance
  for a real work scope; if it feels awkward, revisit.
- [ ] **One single-scroll dashboard beats per-chart pages.** Test:
  build Slice 4 with anchored sections; if you wish for multi-page
  navigation, refactor.
- [ ] **`flow materialise` + external cron is simpler than in-process
  scheduling.** Test: Slice 1 ships the CLI command; iterate on cron
  config in real use.
- [ ] **Shared-password auth is enough for Tailscale-fronted
  access.** Test: Slice 2 enforces `--password` when `--host !=
  127.0.0.1`. If you find yourself bypassing it, the security model
  needs revisiting.

---

## MVP Scope

Each slice ends with a browser-visible chart or an MCP-invokable
tool. No slice is "data layer in isolation."

1. **CLI: `flow materialise`.** Fetch GitHub PRs → write Parquet for
   one hardcoded contract. Tested via external cron locally.
2. **Web: single dashboard, one chart, port + host flags.**
   `flow serve --port 8000 --host 127.0.0.1`. One Vega-Lite
   scatterplot from Parquet via DuckDB. **Refuses to start with
   `--host != 127.0.0.1` unless `--password` is provided** (env var
   or CLI flag).
3. **Web: window controls + aging.** Same page, anchored sections
   (`#cycle-time`, `#aging`). HTMX swaps on filter change.
4. **Web: team filter + forecast.** Add `#forecast` and `#cfd`
   sections. Metadata extraction from labels via contract YAML.
5. **ETL: Jira source.** Same dashboard, Jira-shaped contract.
6. **Web: contract switcher + editor.** Dropdown for switching active
   contract; YAML textarea + validate/refresh buttons.
7. **MCP server.** `flow mcp --data-dir ~/flowmetrics-X` connects via
   stdio. Tools, resources, prompts (list below). Configured in
   Claude Desktop.
8. **Multi-instance hardening.** `--data-dir`, `--contracts-dir`
   flags so multiple instances coexist. CSRF on state-changing
   routes, basic CSP headers, daily Parquet backup script,
   token-redaction in logs.

### MCP surface (first cut)

```
Tools:
  aggregate(contract, since, until, group_by, filters)
  forecast_when_done(contract, items, training_window, filters)
  forecast_how_many(contract, days, training_window, filters)
  refresh(contract)          # invokes `flow materialise` for the contract
  explain_item(contract, source, item_id)

Resources:
  flowmetrics://contracts                       (list)
  flowmetrics://contracts/{id}/dashboard        (snapshot)
  flowmetrics://contracts/{id}/work-items       (paginated)
  flowmetrics://contracts/{id}/runs/latest      (last ETL manifest)

Prompts:
  weekly-standup-summary
  retro-on-last-iteration
  explain-this-PR <url>
```

Each instance has its own MCP server entry in Claude Desktop's
config; agent sees them as separate servers
(`flowmetrics-personal`, `flowmetrics-work-teamA`, etc.).

---

## Not Doing (and Why)

- **OIDC / SSO / role-based access.** Still one operator. A shared
  password protects the network surface; that's enough for solo.
- **Multi-tenancy / shared deployments.** Multiple instances =
  multiple processes + data dirs. No tenant-isolation logic needed
  inside one process.
- **GitHub App + Jira OAuth.** PAT in OS keyring per instance. Solo
  operator = solo secret model.
- **Public REST API + versioning + Hyrum-aware headline split.** MCP
  is the agent surface. Web UI calls internal endpoints not bound by
  external compat.
- **In-process scheduler (APScheduler).** External cron /
  systemd-timer / k8s-cronjob is more Unix-y and matches "scheduler
  for cloud" framing.
- **Per-chart pages / multi-page navigation.** One page, anchored
  sections.
- **Rate limiting on the runtime.** No untrusted callers.
- **K8s manifests, multi-stage Docker.** Tailscale + a VM + cron + a
  Python venv is enough. If a work deployment needs Docker later,
  a single `Dockerfile` ships in an afternoon.
- **Audit log.** No multi-user surface to audit.
- **Bulk export, API keys, agent confirmation tokens.** MCP
  local-stdio doesn't need them.

---

## Open Questions

- **Backup target.** Daily Parquet rsync to `~/Backups/flowmetrics-X/`
  is enough for personal. For work instance, S3-compatible or shared
  NFS? Deferred until you stand up the work instance.
- **Dashboard data refresh in browser.** Server-Sent Events for live
  updates after ETL runs, OR HTMX polling every 60s, OR manual page
  reload? Lean: nothing; the dashboard is "what the last cron tick
  produced." Reload is fine.
- **Schema evolution across instances.** Two instances on different
  `flow` binary versions reading same Parquet — supported? Lean:
  Parquet schema-on-write handles missing columns as NULL; safe.

---

## Decisions locked in this round

- **Save name:** `flowmetrics-single-operator-multi-instance.md` (not
  "personal" — undersells the work-context).
- **Slice 2 enforces shared-password** when bound off-localhost. No
  way to expose the dashboard to Tailscale without a password.
- **External cron** (`flow materialise <contract>`) — not
  APScheduler.
- **Single-page dashboard** with anchored sections — not per-chart
  pages.
- **`--port`, `--host`, `--data-dir`, `--contracts-dir`** are
  first-class CLI flags. Multi-instance is a v1 reality.
- **MCP first-class** alongside the dashboard, not deferred.
