# SPEC: flowmetrics warehouse app (v1 — single-operator, multi-instance)

> **Status.** v1 draft. Aligned to the one-pager at
> `docs/ideas/flowmetrics-single-operator-multi-instance.md`.
> Previous multi-user platform draft archived at
> `docs/SPEC-warehouse-app-v0-multi-user.md.archive` — reference only.
> Load-bearing decisions marked **[D-N]**.

> **Defaults committed for v1:**
>
> - **[D-1]** One operator (you). Multiple instances supported via
>   separate processes + data dirs; no in-process multi-tenancy.
> - **[D-2]** External cron / systemd-timer / k8s-cronjob drives ETL.
>   The runtime never schedules its own work.
> - **[D-3]** Single-page web dashboard with anchored sections.
> - **[D-4]** MCP server is first-class alongside the web UI, not
>   deferred. Stdio-only; configured per-instance in Claude Desktop.
> - **[D-5]** Network access requires `--password` when bound
>   off-localhost. No OIDC, no roles, no users table.

---

## 1. Goals & non-goals

### Goals

1. **Move flow-metrics computation off interactive runtime.** The
   `flow materialize` CLI is invoked by external cron; it fetches
   from GitHub/Jira and writes Parquet. The runtime serves charts
   and MCP queries from that Parquet — never by calling external
   APIs during a request.
2. **Make slicing dynamic.** Team, initiative, severity, time window
   — all queryable from one fact table without re-fetching anything.
3. **Make MCS fast.** Sub-second forecast responses at standard
   sample sizes; the slow part is ETL, not the simulator.
4. **Serve both eyes and agents.** Web dashboard for at-a-glance
   reading; MCP server for "ask Claude about this." Both surfaces
   on the same data.
5. **Run multiple instances side-by-side.** Personal + work + work-
   team-A as separate processes, separate data dirs, separate
   contracts. Same binary.

### Non-goals (v1)

- **Multi-user.** No second human user. Add when one appears.
- **Multi-tenancy inside one process.** Multiple deployments =
  multiple processes.
- **Public REST API + versioning policy.** MCP is the agent surface;
  the web UI uses private internal endpoints.
- **GitHub App / Jira OAuth.** PAT per instance in OS keyring.
- **Real-time / webhook updates.** Refresh is cron-only.
- **Custom dashboard builder.** Fixed report types; MCP gives custom
  queries.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Browser (you, possibly over Tailscale or Caddy)                │
│  ─────────────────────────────────────────────────────────────  │
│  Single-page dashboard, anchored sections, HTMX swaps           │
└────────────────────────────────┬────────────────────────────────┘
                                 │ HTTPS (via Caddy) or HTTP (local)
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    RUNTIME PROCESS (per instance)                │
│                    flow serve --port N --host …                  │
│                    --data-dir … --workflows-dir …                │
│  ─────────────────────────────────────────────────────────────  │
│  /              single dashboard page                            │
│  /api/internal  HTMX fragment endpoints (private; not versioned) │
│  /healthz       liveness                                         │
│                                                                  │
│  Reads only. Never calls GitHub/Jira. Hot in memory:            │
│    • DuckDB connection (pooled)                                 │
│    • Contracts YAML (parsed, validated)                         │
│    • Throughput-array LRU per (filter, window)                  │
└────────────────────────────────┬────────────────────────────────┘
                                 │ reads
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                    DATA LAYER (Parquet store)                   │
│  ─────────────────────────────────────────────────────────────  │
│  $DATA_DIR/                                                      │
│    work_items/    contract_id=X/year=Y/month=M/day=D/items.parquet    │
│    transitions/   contract_id=X/year=Y/month=M/day=D/items.parquet    │
│    contracts/     {hash}.yaml + {hash}.metadata.json            │
│    runs/          contract_id=X/run_id=…/manifest.json          │
└────────────────────────────────▲────────────────────────────────┘
                                 │ writes Parquet (atomic rename)
                                 │
┌─────────────────────────────────────────────────────────────────┐
│             flow materialize <contract>  (separate command)      │
│             driven by external cron / systemd-timer / k8s-cron   │
│  ─────────────────────────────────────────────────────────────  │
│  One-shot ETL:                                                   │
│    1. Read contract YAML                                         │
│    2. Fetch from GitHub/Jira (cached, paginated)                 │
│    3. Replay events against contract stages                      │
│    4. Build work_items + transitions rows                        │
│    5. Atomic write to Parquet                                    │
│    6. Append run manifest                                        │
│    7. Exit                                                       │
│                                                                  │
│  Holds secrets in memory only for run duration. PAT from OS      │
│  keyring (or $GITHUB_TOKEN env).                                 │
└────────────────────────────────┬────────────────────────────────┘
                                 │ HTTPS
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│  GitHub API (GraphQL + REST)      |    Jira API (REST)          │
└─────────────────────────────────────────────────────────────────┘

╔═══════════════════════════════════════════════════════════════════╗
║                MCP SERVER (separate stdio process)                 ║
║                flow mcp --data-dir … --workflows-dir …             ║
║  ───────────────────────────────────────────────────────────────  ║
║  Configured in Claude Desktop / Cursor / Zed.                     ║
║  Reads same Parquet store. Tools, resources, prompts.             ║
║  No network listener; stdin/stdout only.                          ║
╚═══════════════════════════════════════════════════════════════════╝
```

### Why ETL is a separate command

Three concrete benefits over an in-process scheduler:

- **The runtime is stateless.** Restarting `flow serve` doesn't reset
  any cron clock; the scheduler is OS-managed.
- **Scaling is cloud-portable.** `flow materialize` runs as a
  k8s CronJob, a Lambda on a schedule, a Vercel cron — none of which
  the runtime knows about.
- **Failure isolation.** A misbehaving ETL run can't crash the
  serving process; conversely a runtime restart doesn't delay the
  next refresh.

### Why multiple instances, not multi-tenancy

A single binary running N times against N data dirs is operationally
simpler than one process with N tenants:

- No shared cache. No tenant-isolation bugs. No cross-tenant data
  leakage by construction.
- Each instance can be on a different version of the binary.
- Backup, restore, decommission are file-system operations.
- The MCP server gets a per-instance entry in Claude Desktop's
  config; agents see them as distinct servers.

---

## 3. Operator (singular)

You. The user named in the one-pager. Both admin and viewer; no
distinction. Possibly accessing the dashboard from multiple devices
via Tailscale.

---

## 4. Data model

### 4.1 `work_items` (fact table)

One row per piece of work. Denormalised. Wide. Hive-partitioned by
`contract_id` and year/month so DuckDB prunes irrelevant partitions.

| Group | Column | Type | Source |
|---|---|---|---|
| Identity | `source` | enum | github / jira |
| | `repo` | string | for URL only; not a primary slice |
| | `item_id` | string | PR# / issue# / jira-key |
| | `item_type` | string | pr / issue / epic / … |
| | `title` | string | |
| | `url` | string | |
| | `author` | string | login or accountId |
| | `is_bot` | bool | derived from contract exclusions |
| Lifecycle | `created_at` | timestamp | |
| | `completed_at` | timestamp | null if in-flight |
| | `closed_at` | timestamp | may differ from completed |
| | `cycle_time_days` | double | derived |
| | `age_days` | double | now − created_at for in-flight |
| Stage durations | `time_in_<stage>_h` | double | one column per contract stage |
| | `total_active_h` | double | Σ wip-stage durations |
| | `total_wait_h` | double | Σ non-wip-stage durations |
| | `flow_efficiency` | double | active / (active + wait) |
| Phase durations | `pickup_time_h` | double | PR open → first non-author review |
| | `review_time_h` | double | first review → merge |
| | `post_approval_wait_h` | double | last approval → merge |
| Slicing metadata | `team` | string | from contract metadata mapping |
| | `initiative` | string | |
| | `severity` | string | |
| | `area` | string | |
| | `epic_key` | string | parent epic / closing issue |
| | `linked_keys` | list[string] | cross-source links |
| Activity | `commit_count` | int | |
| | `review_count` | int | |
| | `comment_count` | int | |
| | `last_activity_at` | timestamp | |
| Provenance | `contract_id` | string | hash of contract YAML |
| | `materialized_at` | timestamp | when ETL produced this row |
| | `run_id` | string | ETL run that produced this row |

### 4.2 `stage_transitions` (long table)

Source of truth for per-stage durations. The fact table's `time_in_*`
columns are derived from this; the contract can change without
re-fetching from GitHub/Jira.

| Column | Type |
|---|---|
| `source` | enum |
| `item_id` | string |
| `entered_at` | timestamp |
| `exited_at` | timestamp (null if still in stage) |
| `stage` | string |
| `is_wip` | bool |
| `signal` | string |
| `contract_id` | string |

### 4.3 Throughput — computed on demand

Throughput for MCS is computed on demand from the fact table:

```sql
SELECT date_trunc('day', completed_at) AS d, COUNT(*) AS completions
FROM work_items
WHERE contract_id = $contract
  AND completed_at BETWEEN $training_start AND $training_end
  AND team IN ('platform', 'data')
  AND NOT is_bot
GROUP BY d ORDER BY d
```

DuckDB scans a 100k-item fact table for one contract + filter in
~5–20ms. The MCS step (`np.random.choice` from the array) adds ~10ms.
Forecast budget: well under 80ms p50 without pre-computation.

Throughput arrays are cached in a process-local LRU keyed by
`(contract_id, filter_hash, training_window)`. Cache key includes
the contract's `run_id` so it invalidates when ETL writes a new run.

### 4.4 Contracts vs Views

Two completely different lifecycles. Worth naming so they don't get
conflated.

| Aspect | **Contract** | **View** |
|---|---|---|
| Lifetime | Durable; weeks to months | Ephemeral; one query / one UI interaction |
| Defines | Stages, metadata extraction, exclusions, scope of what ETL fetches | Time window + slice filters for *this* read |
| How edited | YAML in `data/contracts/` (or web UI textarea) | URL params, UI controls, MCP tool args |
| Triggers | ETL re-run | Nothing — pure read |
| Versioning | Hashed; old data preserved under old hash | Not versioned |
| Examples | "Added Design stage" / "Drop dependabot" | "Last 14 days, teams a+b" |

**The runtime guarantee.** Any report, any forecast, any aggregation
accepts a (window, filter) pair and operates on that slice without
touching the contract. The contract defines *what was fetched and how
it's parsed*; the view defines *what the operator wants to see right
now*.

---

## 5. Contract format

```yaml
contract:
  name: acme-platform-team
  version: 3
  inherits: org-base    # optional; resolved from same contracts dir

  scope:
    source: github      # github | jira
    query: "org:acme is:pr is:merged repo:acme/svc-a repo:acme/svc-b"
    # OR for Jira:
    # source: jira
    # project: PLATFORM
    # jql: 'project = PLATFORM AND statusCategory = Done'

  stages:
    - { name: Triage,        query: "label:triage",            wip: false }
    - { name: Design Doing,  query: "label:design/doing",      wip: true  }
    - { name: Design Done,   query: "label:design/done",       wip: false }
    - { name: Build Doing,   query: "label:build/doing",       wip: true  }
    - { name: Build Done,    query: "label:build/done",        wip: false }
    - { name: In Review,     query: "is:open review:none",     wip: false }
    - { name: Approved,      query: "is:open review:approved", wip: false }
    - { name: Merged,        query: "is:merged",               wip: false }

  metadata:
    team:
      strategy: label_pattern
      pattern: "team-(.+)"
      default: "unowned"
    initiative:
      strategy: label_pattern
      pattern: "initiative-(.+)"
    severity:
      strategy: label_pattern
      pattern: "sev-(\\d+)"

  exclusions:
    - "user:dependabot[bot]"
    - "user:renovate[bot]"
    - "created:<2024-01-01"

  refresh:
    # Schedule is metadata only — external cron actually invokes the
    # `flow materialize` command. This block documents the intended
    # cadence for the operator's reference.
    intended_schedule: "0 6 * * *"
    in_flight_max_age_days: 180
    history_window_days: 90
```

### Supported predicate vocabulary

Stages and exclusions accept the subset of GitHub search syntax whose
history we can replay locally from timeline events:

| Predicate | Replayed via |
|---|---|
| `label:X` | LabeledEvent / UnlabeledEvent |
| `is:open` / `is:closed` | ClosedEvent / ReopenedEvent |
| `is:draft` | ReadyForReview / ConvertToDraft |
| `is:merged` | MergedEvent |
| `assignee:X` | AssignedEvent / UnassignedEvent |
| `review:approved` / `review:changes_requested` / `review:none` | PullRequestReview state |
| `user:X` | item.author (static) |
| `created:<date` / `>date` | item.created_at (static) |
| Conjunctions (`AND`, space) | local conjunction |
| Negations (`-label:X`) | local negation |

Unsupported predicates (`comments:>N`, full-text, regex) are rejected
at contract validation.

### Inheritance & override resolution

When a contract has `inherits: org-base`:

- Org-base's stages provide the stage taxonomy.
- Child can redefine a stage's `query` but not its `name`.
- Child can add stages at the end; order matters for assignment.
- Child can add metadata extractors but not remove ones from parent.
- Stage assignment when multiple match: **rightmost in declared order
  wins**.

### Validation

Run at contract-load time:

- All stage queries use only supported predicates.
- Stage names unique.
- Metadata extractor strategies recognised.
- Exclusions valid.
- `inherits` resolves.

Invalid contracts are rejected with line-level error messages.
`flow materialize` refuses to run; the web UI continues serving the
last good contract.

---

## 6. ETL command

### 6.1 Invocation

```
flow materialize <contract-name> \
    --data-dir $DATA_DIR \
    --workflows-dir $CONTRACTS_DIR
```

- One-shot. Exits 0 on success, non-zero on failure.
- Reads contract YAML; fetches scope; replays events; writes Parquet
  atomically; appends a run manifest.
- Holds source credentials in memory only for the run duration.
- Token from OS keyring (lookup key derived from contract scope) or
  `$GITHUB_TOKEN` / `$JIRA_TOKEN` env vars.

### 6.2 External scheduling

Three supported patterns; pick whichever fits your deployment.

**cron** (laptop or VM):

```cron
# m h dom mon dow command
0 6 * * *  cd /home/me/flowmetrics && /usr/local/bin/flow materialize platform-team
30 6 * * * cd /home/me/flowmetrics && /usr/local/bin/flow materialize personal-projects
```

**systemd-timer** (Linux server):

```ini
# flow-materialize@.service
[Service]
Type=oneshot
ExecStart=/usr/local/bin/flow materialize %i

# flow-materialize@.timer
[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true
```

**k8s CronJob** (cloud):

```yaml
apiVersion: batch/v1
kind: CronJob
metadata: { name: flow-materialize-platform-team }
spec:
  schedule: "0 6 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          containers:
            - name: flow
              image: flowmetrics:latest
              command: ["flow", "materialize", "platform-team"]
              env: [...secrets from secret manager...]
              volumeMounts: [...persistent volume for $DATA_DIR...]
```

The runtime is unaware of which is in use.

### 6.3 Refresh tiering

`flow materialize` applies the same tiering each run:

| Tier | Items | Behaviour |
|---|---|---|
| **Frozen** | `completed_at < window_start` and `contract_id` unchanged | Read from existing Parquet, never re-fetched, never re-derived |
| **Closed-recent** | `completed_at` within last `history_window_days` | Re-derived from cache (events may have been backfilled) |
| **In-flight** | `completed_at is null` | Re-fetched and re-derived |

Cache key includes `(scope_query, window, source)`. Cached responses
are permanent for closed windows; in-flight items use a fresh fetch
every run.

### 6.4 Failure modes

| Failure | Behaviour |
|---|---|
| Invalid contract syntax | Exit non-zero before any network call; cron logs the failure |
| GitHub rate limit | Wait for reset OR write partial manifest + exit; next scheduled run retries |
| Jira auth expired | Exit non-zero with clear message; next run picks up after rotation |
| Unknown event type in source response | Log WARN, ignore the event; don't fail the run |
| Parquet write fails partway | `.tmp` files cleaned up on next run start |

---

## 7. Runtime — `flow serve`

### 7.1 Invocation

```
flow serve --port 8000 --host 127.0.0.1 \
    --data-dir $DATA_DIR \
    --workflows-dir $CONTRACTS_DIR \
    [--password $PASSWORD | $FLOW_PASSWORD]
```

- `--host 127.0.0.1` is the default. Refuses to start if `--host` is
  set to anything else (any LAN/0.0.0.0 bind, IPv6 etc.) **unless**
  `--password` or `$FLOW_PASSWORD` is set. No exceptions.
- `--port` defaults to 8000.
- `--data-dir` and `--workflows-dir` default to `./data` and
  `./contracts` respectively; intended override per instance.

### 7.2 Performance budget

| Operation | p50 | p99 |
|---|---|---|
| Dashboard page render (server-side) | 100ms | 400ms |
| Single chart-data query (DuckDB scan + aggregate) | 50ms | 200ms |
| MCS forecast at 10k sims, 30-day horizon | 80ms | 250ms |

All measured cold-cache assuming a 100k-item fact table on local SSD.

### 7.3 Web UI shape — composable metric components

Each metric is one reusable component used in two contexts:

- **Tile mode** — embedded as a dashboard cell. Compact: headline
  number, primary chart at small scale, 1–3 summary stats, a
  "Details →" link. No interpretation prose; no methodology
  explanation; no actions panel.
- **Detail mode** — its own page. The same tile at the top (now at
  full scale), then interpretation, then methodology links, then a
  reserved actions section.

Both modes are rendered from the **same underlying data and the same
Vega-Lite spec**. Only the surrounding chrome differs. The HTMX
fragment endpoint takes a `mode=tile|detail` param; behind the
endpoint, a shared `render_metric(metric, view, mode)` function
produces the right shape.

#### 7.3.1 Routes

```
GET /                              → dashboard.html (all tiles in a grid)
GET /metrics/cycle-time            → detail page for cycle time
GET /metrics/aging                 → detail page for aging
GET /metrics/cfd                   → detail page for CFD
GET /metrics/forecast              → detail page for WWIBD
GET /items/{source}/{item_id}     → per-item lineage view
GET /admin                         → contract switcher + editor (Slice 6+)
```

Every URL accepts the standard view params (window, filters,
contract). Deep-linkable: every state is in the query string.

#### 7.3.2 Tile structure

Every tile renders the same five elements:

```
┌──────────────────────────────────────────────┐
│  METRIC NAME                       Details → │  ← header + link
│  ─────────────────────────────────────────   │
│                                              │
│   <headline number or sentence>              │  ← one line, big
│                                              │
│   <primary chart, ~360×240px>                │  ← Vega-Lite
│                                              │
│   <up to 3 summary stats>                    │  ← e.g. P50, P85
│                                              │
└──────────────────────────────────────────────┘
```

The "Details →" link points to the corresponding detail page,
preserving the current view params. HTMX swap on any filter change
re-renders only the tile body (header/link unchanged).

#### 7.3.3 Detail-page structure

```
┌──────────────────────────────────────────────┐
│  <tile component, full scale, ~960×480px>    │  ← same tile, expanded
└──────────────────────────────────────────────┘

  ## How to read this
  <prose; explains what the chart is showing, how to interpret
  variance, what "good" looks like, links to METRICS.md / TUNING.md>

  ## Caveats
  <bulleted list — same caveats the current interpretation layer
  generates, e.g. "Percentiles are empirical from the in-window
  completers", "Items in 'Open' often sit in intake queue, not
  active development">

  ## Methodology
  <links to docs/METRICS.md anchors; the math; the source-of-truth
  fields used>

  ## Actions
  <reserved section; v1 ships empty with a small "Coming soon"
  caption. v2 might host AI-suggested experiments, filter-preset
  shortcuts, links to expand the window or change the contract.>
```

The actions section is empty in v1 on purpose. It's the placeholder
for whatever ends up being the "now what?" surface — but committing
to specific actions before there's a real use case is exactly the
speculative scope this whole spec rewrite cut.

#### 7.3.4 Component inventory

Six metric components, each a Jinja partial + a Python renderer:

| Component | Tile headline | Tile chart | Detail extras |
|---|---|---|---|
| `cycle_time` | "P50 1.8d, P85 13.3d (43 items)" | scatterplot | interpretation + caveats + methodology + actions |
| `aging` | "3 items past P85, 1 past P95" | WIP aging chart | per-state table + interpretation + caveats + methodology + actions |
| `cfd` | "Widest band: Awaiting Review (47 items)" | CFD bands | per-stage trend lines + interpretation + caveats + methodology + actions |
| `forecast_when_done` | "85% confident: 25 items by May 31" | histogram | percentile table + training-window detail + interpretation + caveats + methodology + actions |
| `forecast_how_many` | "85% confident: ≥17 items in 14 days" | histogram | percentile table + training-window detail + interpretation + caveats + methodology + actions |
| `work_items_table` | (none — table only on detail) | (none) | filtered/sortable table + per-item link |

`work_items_table` is detail-only; the dashboard doesn't show a
table because it doesn't fit a tile's purpose ("at a glance"). The
table lives on its own page reachable from "Browse items →" at the
top of the dashboard.

#### 7.3.5 Layout

Dashboard:

```
┌──────────────────────────────────────────────┐
│  FILTER BAR (sticky)                          │
│  contract: [▼]  since: [ ]  until: [ ]        │
│  team: [▼]  initiative: [▼]  bots: [☐]        │
└──────────────────────────────────────────────┘
┌─────────────────┬─────────────────────────────┐
│  cycle_time     │  aging                       │
├─────────────────┼─────────────────────────────┤
│  cfd            │  forecast_when_done          │
├─────────────────┼─────────────────────────────┤
│  forecast_how_many                             │
└─────────────────┴─────────────────────────────┘
                              [Browse items →]
```

Two-column grid on wide screens; single column on narrow. The filter
bar is sticky so it remains visible while scrolling.

Detail:

```
┌──────────────────────────────────────────────┐
│  FILTER BAR (sticky, same as dashboard)       │
└──────────────────────────────────────────────┘
┌──────────────────────────────────────────────┐
│  <metric tile, full-width>                   │
│  How to read · Caveats · Methodology · Actions│
└──────────────────────────────────────────────┘
                              [← Back to dashboard]
```

A filter change on either layout updates the URL and re-renders
affected tiles via HTMX.

### 7.4 Internal endpoints (HTMX fragments)

```
GET /api/internal/cycle-time?contract=…&since=…&until=…&filter[team]=…
GET /api/internal/aging?contract=…&filter[…]=…
GET /api/internal/cfd?contract=…&since=…&until=…
GET /api/internal/forecast/when-done?contract=…&items=…&training_window_days=…&filter[…]=…
GET /api/internal/forecast/how-many?contract=…&days=…&training_window_days=…&filter[…]=…
GET /api/internal/items?contract=…&cursor=…&limit=…
GET /api/internal/dimensions?contract=…
POST /api/internal/contracts/{id}/refresh    # invokes flow materialize as a subprocess
PUT  /api/internal/contracts/{id}            # YAML upload + validate; admin = operator
```

These endpoints are **private**. They're not OpenAPI-versioned, not
intended for external consumers, and may change shape without notice.
The MCP server (§8) is the supported agent surface.

### 7.5 The "explain why this item is in this stage" view

Every fact-table row must explain itself. The per-item page shows:

- Current stage + which event drove the most recent transition
- Full transition history
- All metadata extractions and which event/label produced each
- All exclusions evaluated (passed/failed each one)
- Direct link to GitHub/Jira for ground truth

This is what makes the contract debuggable.

---

## 8. MCP server — `flow mcp`

### 8.1 Invocation

```
flow mcp --data-dir $DATA_DIR --workflows-dir $CONTRACTS_DIR
```

Stdio-only. Spawned by an MCP client (Claude Desktop, Cursor, Zed,
Continue). Reads same Parquet store as the web runtime. No network
listener.

Example Claude Desktop config:

```json
{
  "mcpServers": {
    "flowmetrics-personal": {
      "command": "flow",
      "args": ["mcp", "--data-dir", "/Users/me/flowmetrics/personal"]
    },
    "flowmetrics-work-platform": {
      "command": "flow",
      "args": ["mcp", "--data-dir", "/Users/me/flowmetrics/work-platform"]
    }
  }
}
```

Agent sees each instance as a distinct MCP server.

### 8.2 Tools

```
aggregate(contract: str, since: date, until: date,
          group_by: "team"|"initiative"|"severity"|"item_type"|"author"|null,
          filters: dict[str, list[str]] = {},
          metric: "cycle_time"|"flow_efficiency"|"throughput" = "cycle_time",
          percentiles: list[int] = [50, 70, 85, 95]) -> AggregateResult

forecast_when_done(contract: str, items: int, start: date,
                   training_window_days: int = 30,
                   filters: dict[str, list[str]] = {}) -> ForecastResult

forecast_how_many(contract: str, days: int, start: date,
                  training_window_days: int = 30,
                  filters: dict[str, list[str]] = {}) -> ForecastResult

refresh(contract: str) -> RefreshResult
  # Invokes `flow materialize` as a subprocess. Streams progress to
  # the MCP client. Idempotent — concurrent calls coalesce.

explain_item(contract: str, source: "github"|"jira",
             item_id: str) -> ItemLineage
```

All tool returns are typed JSON schemas declared in the MCP server
manifest. The protocol's structured returns subsume the Hyrum-aware
headline/template split — agents read fields directly, not strings.

### 8.3 Resources

```
flowmetrics://contracts                                  → list of contracts
flowmetrics://contracts/{id}/dashboard                   → JSON snapshot of dashboard data
flowmetrics://contracts/{id}/work-items?since=…&until=…  → paginated list
flowmetrics://contracts/{id}/runs/latest                 → last ETL manifest
flowmetrics://contracts/{id}/yaml                        → contract YAML text
```

### 8.4 Prompts

```
weekly-standup-summary       → templated summary of last 7 days for a contract
retro-on-last-iteration      → templated retro digest
explain-this-PR <url>        → look up a PR in any contract's work_items
```

Prompts return MCP `Message[]` that an agent can feed straight back
to the model.

### 8.5 What the MCP server is NOT

- Not networked. Stdio only. If you want remote agent access, run
  the agent on the same machine (or via Tailscale to the same
  machine).
- Not multi-tenant. Each instance = one MCP server entry in the
  agent client's config.
- Not authenticated. Local-process trust model — the agent client is
  already running with your privileges.

---

## 9. Security model

Slimmed for single-operator + possibly-network-exposed reality.

### 9.1 Threat actors

| Actor | Capability | Concerns |
|---|---|---|
| External attacker (no Tailscale access) | None | App default-binds to 127.0.0.1; not exposed |
| External attacker (with Tailscale access) | Can hit the dashboard if you've exposed it | Password gate on non-localhost binds |
| Malicious agent | Has MCP stdio access (already on your machine) | Out of scope — agent client trust is the OS's job |
| Compromised host | Has filesystem + memory access | PAT in OS keyring; no plain-text on disk |

### 9.2 Source authentication

- GitHub: PAT in OS keyring or `$GITHUB_TOKEN` env. Per-instance.
- Jira: API token in OS keyring or `$JIRA_TOKEN` env. Per-instance.
- Tokens read into memory only during `flow materialize` run. Never
  written to Parquet, never returned in API responses, never logged.

### 9.3 Network access

- `flow serve` defaults to `--host 127.0.0.1`.
- Any other bind requires `--password` or `$FLOW_PASSWORD`. The
  password gates HTTP Basic auth on every request. Password is
  bcrypt-hashed in memory; only the env-var / flag holds plaintext.
- For Tailscale: bind to `--host 0.0.0.0 --port 8000` with a
  password; Tailscale ACLs handle who can reach the port.
- For Caddy in front: Caddy terminates TLS, forwards to the app on
  127.0.0.1; the app binds locally. No password needed in that
  topology (Caddy can do its own auth if you want).

### 9.4 Web hardening (Slice 8)

- CSRF tokens on state-changing routes (`PUT /api/internal/contracts/*`,
  `POST /api/internal/contracts/*/refresh`).
- CSP header allowing only the Vega-Lite CDN script tags.
- Jinja autoescape on; `yaml.safe_load` for contract parsing.
- Logging middleware redacts `gh[ps]_*` and JWT-shaped strings before
  write.
- SQL queries parametrised through a single query module.

### 9.5 What's NOT in the security model

- OIDC / SSO.
- User accounts, role-based access.
- API keys for agents (MCP is local-stdio).
- Audit log of admin actions (no other admins).
- Multi-tenant isolation (separate processes).
- Rate limiting (no public callers).

---

## 10. Deployment

Three shapes, all from the same `flow` binary.

### 10.1 Laptop (single instance, single operator)

```
$ flow materialize personal-projects    # run by cron, /Users/me/.crontab
$ flow serve --port 8000 --data-dir ./data --workflows-dir ./contracts
$ flow mcp --data-dir ./data --workflows-dir ./contracts   # spawned by Claude Desktop
```

All three commands share `./data` and `./contracts`. No process
coordination required because each does narrow things to its own
files (ETL writes Parquet, runtime reads, MCP reads).

### 10.2 Laptop + multiple instances

```
~/flowmetrics-personal/
~/flowmetrics-work-platform/
~/flowmetrics-work-data/
```

Each directory has its own `data/`, `contracts/`, `crontab` entries,
and (in Claude Desktop) MCP server config entry. `flow serve` runs
once per directory on different ports.

### 10.3 VM behind Tailscale + Caddy

```
my-vm:
  /opt/flowmetrics/data/
  /opt/flowmetrics/contracts/
  systemd-timer: flow-materialize@{contract}.timer for each contract
  systemd-service: flow-serve.service binding 127.0.0.1:8000
  Caddy: TLS termination on :443 → 127.0.0.1:8000
```

Tailscale provides identity (only Tailscale-authenticated devices
reach the VM). Caddy handles TLS. The app stays on `127.0.0.1`.

---

## 11. Operations

### 11.1 Observability

- `/healthz` — process liveness (no auth).
- `/readyz` — 503 if no contract has a successful run within
  `2 × intended_schedule` interval.
- Structured logs to stdout (JSON). Cron / journald captures.
- ETL manifests at `$DATA_DIR/runs/{contract}/run_id=…/manifest.json`
  for per-run inspection.

### 11.2 Backup

Daily rsync of `$DATA_DIR` to a chosen target. Script ships in
Slice 8. Contracts are also versioned in a git repo separate from
the application source (the operator's choice of remote).

### 11.3 Schema evolution

- Adding a column to `work_items` → Parquet handles natively (old
  rows read as NULL).
- Adding a stage to a contract → next ETL run produces new column on
  rows for that contract; rows from other contracts remain NULL.
- Renaming a stage → re-derive from `stage_transitions` (cheap).
- Removing a stage → row's `time_in_<stage>_h` remains as a historical
  artefact; new ETL writes NULL.

---

## 12. Out of scope (v1, explicit)

- OIDC / SSO / user accounts / RBAC.
- Multi-tenancy inside one process.
- Webhook-driven refresh.
- Custom dashboard builder.
- Public REST API + versioning policy + deprecation procedure.
- GitHub App / Jira OAuth.
- Bulk export / agent API keys / agent confirmation flow.
- K8s manifests as a first-class deliverable. (`flow materialize`
  works as a CronJob; the rest is left to the operator.)
- Real-time / sub-minute dashboards.
- Per-engineer scoring / individual surfacing.
- Source adapters beyond GitHub / Jira.

---

## 13. Open questions

1. **Backup target for the work instance.** S3-compatible or shared
   NFS? Deferred until the work instance exists.
2. **Dashboard data refresh mechanism.** SSE / HTMX polling / manual
   reload? Lean: manual reload — the dashboard is "what the last cron
   tick produced."
3. **Cross-instance schema-version skew.** Two instances on
   different binary versions reading same Parquet — supported via
   schema-on-write NULL semantics. Worth a regression test in
   Slice 8.
4. **MCP `refresh` semantics under concurrent invocation.** Coalesce
   into one running subprocess, or queue? Lean: coalesce, return
   the in-flight run ID.

---

## 14. Migration from current CLI

The existing CLI commands (`flow efficiency`, `flow metric aging`, etc.)
keep working in v1 unchanged. The new commands (`flow materialize`,
`flow serve`, `flow mcp`) are additive. After Slice 4 the existing
commands gain a `--from-warehouse` flag that reads Parquet instead
of fetching live; the live-fetch path remains the default and the
fallback for repos without a materialized contract.

`scripts/generate_samples.py` continues to use the live-fetch path
for the sample regen flow.

---

## 15. Slices

Each slice ends with a browser-visible chart or an MCP-invokable
tool. **No slice is "data layer in isolation."** Slice acceptance
includes a Playwright e2e test asserting what the slice promised
(per `SPEC.md` §6 credibility rule).

### Slice 1 — `flow materialize` writes Parquet

**Goal.** CLI command that fetches GitHub PRs for one hardcoded
contract and writes `work_items.parquet` + `stage_transitions.parquet`.
Tested via external cron locally.

**Acceptance.** Run `flow materialize personal` from cron at 06:00;
on inspection the next morning, Parquet files exist with the
expected rows, and a run manifest is in `data/runs/`. DuckDB can
query the result.

### Slice 2 — `flow serve` one chart with port + password gate

**Goal.** Single dashboard page renders a Vega-Lite cycle-time
scatterplot from the Parquet store. `--port` / `--host` flags.

**Acceptance.** `flow serve --port 8000` shows a scatterplot at
`http://127.0.0.1:8000/`. `flow serve --host 0.0.0.0 --port 8000`
without `--password` exits with a clear error. With `--password`,
the dashboard requires HTTP Basic auth.

**E2E test.** Playwright drives the page, asserts the SVG renders
with the right axis labels, data points appear at expected dates,
P50/P85 lines show, and Basic-auth gating works when bound off-
localhost.

### Slice 3 — Window controls + aging section

**Goal.** Date-window picker drives both `#cycle-time` and a new
`#aging` section. HTMX swaps the affected fragments.

**Acceptance.** Dragging the window control re-renders both charts
within 500ms. Anchor links `#cycle-time` and `#aging` work.

### Slice 4 — Team filter + forecast + CFD

**Goal.** Add `#forecast` and `#cfd` sections. Metadata extraction
runs in ETL so `team` is a column. Filter widget driven by a
`/api/internal/dimensions` call.

**Acceptance.** Selecting "team=platform" filters all four sections.
Forecast renders in <1s with percentile lines.

### Slice 5 — Jira source on the same UI

**Goal.** Same dashboard works against a Jira-shaped contract.
Cross-source stitching (PR closing issue) works inside one contract.

**Acceptance.** A Cassandra-style contract renders all four sections
with sensible data; the explain-item view shows the Jira changelog
trail.

### Slice 6 — Contract switcher + editor

**Goal.** Dropdown for switching active contract. YAML textarea +
"validate" + "refresh" buttons. Refresh invokes `flow materialize`
as a subprocess; streams output to a log panel.

**Acceptance.** Operator edits a contract in browser, sees
line-level validation errors, fixes them, clicks refresh, watches
the run manifest update, sees the charts reflect the change.

### Slice 7 — MCP server

**Goal.** `flow mcp --data-dir …` runs as a stdio MCP server.
Implements the tools, resources, and prompts in §8.

**Acceptance.** Configured in Claude Desktop. Asking *"What's my
flow this week for the platform contract?"* prompts Claude to call
`aggregate(...)` and produces a sensible answer. Forecast tool
returns typed percentiles + histogram data.

### Slice 8 — Multi-instance + hardening

**Goal.** `--data-dir` and `--workflows-dir` flags so multiple
instances coexist. CSRF on state-changing routes. CSP headers.
Token-redaction middleware. Daily Parquet backup script.

**Acceptance.** Two instances run side-by-side on different ports +
data dirs. State-changing routes reject requests without CSRF
tokens. Logs contain no GitHub PAT under any circumstance (tested
by injecting a token into an error path and grep-asserting the log
output). `flow backup --data-dir …` rsyncs Parquet to a target.

---

## 16. Within-slice work order

Inside any slice the order is fixed (per `SPEC.md` §10):

1. Walk the slice as a user — describe the click path before coding.
2. Stub the page — Jinja template with fake data.
3. Wire the runtime endpoint against fake/synthetic Parquet.
4. Wire the ETL to produce real Parquet.
5. Replace fakes with real data top-to-bottom.
6. Refactor (the TDD refactor step, applied to the slice).
7. **Write the e2e test that asserts what the slice promised.** The
   slice is not done until this test exists and passes.

---

**End of v1 draft.** This spec is the working source of truth for
implementation. Edits welcome; please keep §15 acceptance criteria
human-observable.
