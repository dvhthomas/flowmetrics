"""FastAPI application factory for `flow serve`.

The runtime process reads from the Parquet store written by
`flow materialise` (Slice 1). It never calls GitHub or Jira during
a request — all data comes from local Parquet via DuckDB.

Slice 2 ships two routes:
  GET /                     dashboard with the cycle-time tile
  GET /metrics/cycle-time   detail page reusing the same partial

Both routes resolve `contract` from a query string (or, for now,
the first contract found under contracts_dir).

The factory takes data_dir + contracts_dir explicitly so we can
stand up multiple isolated instances per the multi-instance design.
"""

from __future__ import annotations

import secrets
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .contract import ContractError, load_contract
from .windows import Window, last_completed_week, parse_windows
from .web.components.aging import render as render_aging
from .web.components.cfd import render as render_cfd
from .web.components.cycle_time import render as render_cycle_time
from .web.components.forecast import (
    render_how_many as render_forecast_how_many,
    render_when_done as render_forecast_when_done,
)
from .web.components.lifecycle import (
    ItemNotFound,
    render as render_lifecycle,
)
from .web.components.throughput import render as render_throughput
from .web.components.work_items_table import (
    SortDir,
    SortKey,
    render as render_work_items_table,
)

_TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"


def open_warehouse(data_dir: Path) -> duckdb.DuckDBPyConnection:
    """Open an in-memory DuckDB connection with `work_items` and
    `transitions` views registered against the Parquet warehouse
    under `data_dir`.

    Each ETL run writes a fresh per-day snapshot
    (`year={Y}/month={M}/day={D}/items.parquet`). Cross-day re-runs
    accumulate snapshots, so the same item appears in N partitions
    once N days have passed. The views deduplicate at read time so
    every consumer sees one canonical row per
    `(contract_id, source, item_id)` — the LATEST snapshot —
    without losing the on-disk history (useful for future
    "what did aging look like at snapshot X" features).

    `transitions` is similarly deduplicated. Transitions are
    append-only events — same `entered_at` for the same item
    should never collide — but a re-run that re-fetches the same
    item writes identical rows again, so DISTINCT collapses them.

    Both views fall back to empty stubs when the corresponding
    parquet files don't exist yet (fresh install before
    materialise has ever run).
    """
    con = duckdb.connect(":memory:")

    work_items_glob = (data_dir / "work_items" / "**" / "*.parquet").as_posix()
    try:
        # Latest snapshot per (contract_id, source, item_id) by
        # materialised_at. Stable tie-break by run_id keeps the
        # answer deterministic when two snapshots share the exact
        # same materialised_at (rare; same-second re-runs).
        con.execute(
            f"CREATE VIEW work_items AS "
            f"SELECT * EXCLUDE (_dedup_rn) FROM ( "
            f"  SELECT *, ROW_NUMBER() OVER ("
            f"    PARTITION BY contract_id, source, item_id "
            f"    ORDER BY materialised_at DESC, run_id DESC"
            f"  ) AS _dedup_rn "
            f"  FROM read_parquet('{work_items_glob}', hive_partitioning = true)"
            f") WHERE _dedup_rn = 1"
        )
    except duckdb.IOException:
        # No parquet yet — caller is on a fresh install. work_items
        # is required by every component; let this raise.
        raise

    transitions_glob = (
        data_dir / "transitions" / "**" / "*.parquet"
    ).as_posix()
    try:
        # Transitions are append-only stage-entry events; identical
        # rows across snapshots collapse via DISTINCT. No
        # materialised_at column to order by (transitions don't
        # carry one), but exact-row dedup is enough.
        con.execute(
            f"CREATE VIEW transitions AS "
            f"SELECT DISTINCT * FROM read_parquet("
            f"'{transitions_glob}', hive_partitioning = true)"
        )
    except duckdb.IOException:
        con.execute(
            "CREATE VIEW transitions AS "
            "SELECT NULL::VARCHAR AS source, "
            "NULL::VARCHAR AS item_id, "
            "NULL::TIMESTAMP AS entered_at, "
            "NULL::VARCHAR AS stage, "
            "NULL::VARCHAR AS signal, "
            "NULL::VARCHAR AS contract_id "
            "WHERE FALSE"
        )

    return con


# Exported for tests so they can exercise the production read path
# without standing up a full FastAPI app.
open_warehouse_for_test = open_warehouse


class WorkflowView:
    """Per-request workflow handle. Loads the contract once,
    exposes the warehouse, and orchestrates renders. Replaces
    the older `_workflow_context` + `_workflow_slug` + `_workflow_system_label`
    + `load_contract` chain that loaded the contract three times
    per dashboard request.

    Routes pattern:
        view = _open_view(workflow_id, request)
        with view.warehouse() as con:
            cfd = view.render_cfd(con)
        return templates.TemplateResponse(
            request, "cfd_detail.html.jinja",
            {"contract": view.template_context(), "cfd": cfd, ...},
        )
    """

    def __init__(
        self,
        workflow_id: str,
        *,
        contracts_dir: Path,
        data_dir: Path,
        query: dict | None = None,
    ) -> None:
        self.id = workflow_id
        self._data_dir = data_dir
        # One contract load per request. Propagates ContractError
        # so a malformed YAML surfaces as an HTTP 500 with the
        # parser's message — better than the silent degradation
        # the old _workflow_slug/_workflow_system_label helpers
        # had.
        self.contract = load_contract(workflow_id, contracts_dir)
        # Anchor default windows to the data's most recent
        # completion when available — defaults should produce
        # non-empty charts even when the contract's data window
        # is months old. Falls back to today UTC for fresh
        # installs with no data yet.
        today_utc = datetime.now(UTC).date()
        self.data_max_date = self._latest_completion_date()
        anchor = self.data_max_date or today_utc
        self.view_window, self.reference_period = parse_windows(
            dict(query or {}), today=anchor,
        )
        # "Data is stale" diagnostic for the template. True when
        # the warehouse's latest completion is meaningfully
        # before today (>= 2 days) — typical cron lag is one
        # day, so two-day staleness is the noise floor.
        self.data_is_stale = (
            self.data_max_date is not None
            and (today_utc - self.data_max_date).days >= 2
        )

    def _latest_completion_date(self):
        """Most recent completed_at in this workflow's warehouse,
        or None when the warehouse has no completions yet."""
        try:
            con = open_warehouse(self._data_dir)
        except duckdb.IOException:
            return None
        try:
            row = con.execute(
                "SELECT max(CAST(completed_at AS DATE)) "
                "FROM work_items "
                "WHERE contract_id = ? AND completed_at IS NOT NULL",
                [self.id],
            ).fetchone()
            return row[0] if row and row[0] else None
        finally:
            con.close()

    @contextmanager
    def warehouse(self):
        """Yield a DuckDB connection with views registered against
        the warehouse. Same connection every render gets — single
        per-request open, scoped by the `with`."""
        con = open_warehouse(self._data_dir)
        try:
            yield con
        finally:
            con.close()

    def template_context(self) -> dict:
        """Template-side context for "what workflow is this":
        id, decorative slug, plus the current view/reference
        windows expressed BOTH as explicit from/to dates AND as
        anchor+duration. The filter bar's common controls bind
        to anchor + days; advanced controls bind to from/to.

        `is_advanced_window`: True when view and reference don't
        share a `to` date (advanced mode); False when they do
        (common case — both end at the same anchor).
        """
        anchor = self.view_window.to
        return {
            "name": self.id,
            # Human-friendly display name. Templates render
            # `contract.label` in breadcrumbs / home-page lists;
            # `contract.name` stays the URL-safe routing ID.
            "label": self.contract.label or self.id,
            "slug": self._slug(),
            # Advanced/legacy form: explicit dates per window.
            "view_from": self.view_window.from_.isoformat(),
            "view_to": self.view_window.to.isoformat(),
            "ref_from": self.reference_period.from_.isoformat(),
            "ref_to": self.reference_period.to.isoformat(),
            # Common form: shared anchor + per-window durations.
            "anchor": anchor.isoformat(),
            "view_days": self.view_window.days_inclusive,
            "ref_days": self.reference_period.days_inclusive,
            # True when view/reference can't be expressed as
            # shared-anchor + days (i.e. they end on different
            # dates). The template shows the advanced controls
            # by default in that case.
            "is_advanced_window": (
                self.view_window.to != self.reference_period.to
            ),
            # "Showing data anchored to X — N days ago. Cron
            # will refresh, or run `flow materialise`." banner
            # data. None when data is fresh.
            "data_max_date": (
                self.data_max_date.isoformat()
                if self.data_max_date else None
            ),
            "data_is_stale": self.data_is_stale,
            # Quick-range preset that matches the current anchor
            # + durations, so the dropdown shows the right
            # selection after a preset round-trip (instead of
            # reverting to "Custom"). None when no preset
            # matches (data-max-anchored defaults, manually-set
            # custom values, advanced mode).
            "active_preset": self._active_preset(),
        }

    def _active_preset(self) -> str | None:
        """Map (anchor, view_days, ref_days) → the preset name
        whose math produces those values, or None for custom.

        Mirrors the preset table in the filter-bar template +
        the JS `flowmetricsApplyPreset` switch — keep all three
        in sync when adding a preset."""
        if self.view_window.to != self.reference_period.to:
            return None
        anchor = self.view_window.to
        v = self.view_window.days_inclusive
        r = self.reference_period.days_inclusive
        today_utc = datetime.now(UTC).date()
        if anchor == today_utc:
            if v == 7 and r == 7:
                return "last-7-days"
            if v == 14 and r == 14:
                return "last-14-days"
            if v == 30 and r == 14:
                return "last-30-days"
            if v == 90 and r == 14:
                return "last-90-days"
        last_sat = last_completed_week(today=today_utc).to
        if anchor == last_sat:
            if v == 7 and r == 7:
                return "last-week"
            if v == 14 and r == 14:
                return "last-2-weeks"
        return None

    def _slug(self) -> str:
        c = self.contract
        if c.source == "github" and c.repo:
            return c.repo.lower().replace("/", "-").replace(".", "-")
        if c.source == "jira" and c.jira_project:
            return c.jira_project.lower()
        return self.id

    # ---- render orchestration ------------------------------------

    def render_cfd(self, con):
        return render_cfd(
            con, self.id,
            view=self.view_window,
            states=self.contract.states,
        )

    def render_aging(self, con, *, asof=None):
        return render_aging(
            con, self.id,
            asof=asof,
            contract_start=self.contract.start,
            contract_stop=self.contract.stop,
            states=self.contract.states,
            reference=self.reference_period,
        )

    def render_cycle_time(self, con):
        return render_cycle_time(
            con, self.id,
            view=self.view_window,
            reference=self.reference_period,
        )

    def render_throughput(self, con):
        return render_throughput(
            con, self.id,
            view=self.view_window,
            # Contract's materialise window defines what dates
            # the warehouse covers. Days inside = real zeros
            # possible; outside = "no data, backfill needed".
            # The throughput spec renders these differently.
            warehouse_start=self.contract.start,
            warehouse_stop=self.contract.stop,
        )

    def render_forecast_when_done(self, con, *, items: int, start_date=None):
        return render_forecast_when_done(
            con, self.id,
            items=items,
            start_date=start_date,
            reference=self.reference_period,
        )

    def render_forecast_how_many(self, con, *, start_date, end_date):
        return render_forecast_how_many(
            con, self.id,
            start_date=start_date,
            end_date=end_date,
            reference=self.reference_period,
        )

    def render_lifecycle(self, con, *, source: str, item_id: str):
        return render_lifecycle(
            con, self.id, source, item_id,
            reference=self.reference_period,
        )


def create_app(
    *,
    data_dir: Path,
    contracts_dir: Path,
    cache_dir: Path | None = None,
    password: str | None = None,
) -> FastAPI:
    """Build a FastAPI app reading from this data dir.

    Each call returns an isolated app — multiple instances (one per
    contract scope / data directory) share zero state. Mounted as a
    subprocess of uvicorn, started by `flow serve`.

    `cache_dir` is the source-API response cache (used by the
    materialise endpoint when the operator clicks "import data" on
    an empty aging chart). Defaults to the CLI's `DEFAULT_CACHE_DIR`
    if not supplied.

    `password` opt-in: when set, every request requires HTTP Basic
    auth (user='operator', password matches). Used for off-localhost
    binds where Tailscale or Caddy fronts the app.
    """
    if cache_dir is None:
        from .cli import DEFAULT_CACHE_DIR

        cache_dir = DEFAULT_CACHE_DIR
    app = FastAPI(title="flowmetrics", version="1")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

    # `keep_filters` Jinja filter — appends the operator's
    # view/reference window params to a path so the filter
    # survives navigation between pages within a workflow.
    # `@pass_context` lets the filter read `request` (always
    # injected by Jinja2Templates) without each call site
    # passing it explicitly: `{{ some_path | keep_filters }}`.
    #
    # Only the window-control params propagate — NOT arbitrary
    # query keys. Without the whitelist, internal-endpoint
    # params (`workflow=` on the dashboard-tile route) would
    # leak into every navigation URL.
    from jinja2 import pass_context
    from urllib.parse import parse_qsl, urlencode

    _CARRIED_PARAMS = frozenset({
        "anchor", "view_days", "ref_days",
        "view_from", "view_to", "ref_from", "ref_to",
        "preset", "asof",
    })

    @pass_context
    def _keep_filters(ctx, path: str) -> str:
        request = ctx.get("request")
        if request is None:
            return path
        carried = [
            (k, v)
            for k, v in parse_qsl(request.url.query)
            if k in _CARRIED_PARAMS
        ]
        if not carried:
            return path
        sep = "&" if "?" in path else "?"
        return f"{path}{sep}{urlencode(carried)}"

    templates.env.filters["keep_filters"] = _keep_filters

    # Auth dependency — pass-through when no password configured.
    # FastAPI's idiomatic `Depends(...)` lives in the parameter default
    # which trips ruff's B008; bound to a module-level helper here so
    # the lint stays clean and the dependency reads the same way.
    if password:
        _BASIC = HTTPBasic()
        _expected_password = password

        def _require_auth(
            creds: HTTPBasicCredentials = Depends(_BASIC),  # noqa: B008
        ) -> None:
            user_ok = secrets.compare_digest(creds.username.encode(), b"operator")
            pass_ok = secrets.compare_digest(
                creds.password.encode(), _expected_password.encode()
            )
            if not (user_ok and pass_ok):
                raise HTTPException(
                    status_code=401,
                    detail="invalid credentials",
                    headers={"WWW-Authenticate": "Basic"},
                )

        auth_dep = [Depends(_require_auth)]
    else:
        auth_dep = []

    def _open_view(
        workflow_id: str, request: Request | None = None,
    ) -> WorkflowView:
        """Factory: 404 if the contract YAML is missing, else
        return a WorkflowView. Centralizes the (exists-check +
        construct + bind to factory data/contracts dirs) shape
        that every route used to inline. When `request` is
        passed, the view's window kwargs come from
        `request.query_params` — caller-supplied view/reference
        windows override defaults."""
        _ensure_contract_exists(workflow_id)
        query = dict(request.query_params) if request is not None else None
        return WorkflowView(
            workflow_id,
            contracts_dir=contracts_dir,
            data_dir=data_dir,
            query=query,
        )

    def _parse_asof(value: str | None):
        """`?asof=YYYY-MM-DD` → `date(YYYY, MM, DD)`. `None` or
        invalid → `None` (the component defaults to today UTC).
        Defensive: don't 400 on a bad query param, just fall back."""
        if not value:
            return None
        from datetime import date

        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    def _ensure_contract_exists(name: str) -> None:
        """Confirm a contract YAML exists; otherwise 404 with a clear
        message naming the missing artifact."""
        for ext in (".yaml", ".yml"):
            if (contracts_dir / f"{name}{ext}").exists():
                return
        raise HTTPException(
            status_code=404,
            detail=(
                f"contract {name!r} not found under {contracts_dir}/ "
                "(looked for .yaml and .yml). Create a YAML file there "
                "and run `flow materialise`."
            ),
        )

    def _first_contract_or_404() -> str:
        """Pick the first contract under contracts_dir, or 404 with a
        clear message if there are none."""
        candidates = sorted(contracts_dir.glob("*.yaml"))
        if not candidates:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no contracts found under {contracts_dir}/ — "
                    "create a YAML file there and run `flow materialise`."
                ),
            )
        return candidates[0].stem

    def _available_contracts() -> list[str]:
        return sorted(p.stem for p in contracts_dir.glob("*.yaml"))

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def home(request: Request) -> HTMLResponse:
        """Landing page — lists every contract under contracts_dir
        as a link to its dashboard. The brand link in the header
        always lands here so the operator can pivot between
        workflows without dropping into a specific one first."""
        # Build (name, label) pairs so the home list shows the
        # human-friendly label with the routing id as a subtle
        # subtitle. Fall back to name when label is missing.
        workflows: list[dict] = []
        for name in _available_contracts():
            try:
                c = load_contract(name, contracts_dir)
                label = c.label or name
            except ContractError:
                label = name
            workflows.append({"name": name, "label": label})
        return templates.TemplateResponse(
            request,
            "home.html.jinja",
            {
                "title": "flowmetrics",
                "workflows": workflows,
                # Empty contract keeps `_base.html.jinja` header
                # / filter-bar guards happy without implying a
                # current workflow.
                "contract": None,
            },
        )

    @app.get(
        "/workflows/{workflow_id}/metrics/cfd",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def cfd_detail(request: Request, workflow_id: str) -> HTMLResponse:
        view = _open_view(workflow_id, request)
        with view.warehouse() as con:
            cfd = view.render_cfd(con)
        return templates.TemplateResponse(
            request,
            "cfd_detail.html.jinja",
            {
                "title": f"Cumulative Flow — {workflow_id}",
                "metric_name": "Cumulative Flow",
                "contract": view.template_context(),
                "available_contracts": _available_contracts(),
                "view": {"since": None, "until": None},
                "cfd": cfd,
            },
        )

    @app.get(
        "/api/internal/cfd",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def cfd_fragment(request: Request, workflow: str) -> HTMLResponse:
        view = _open_view(workflow, request)
        with view.warehouse() as con:
            cfd = view.render_cfd(con)
        return templates.TemplateResponse(
            request,
            "_partials/cfd_chart_fragment.html.jinja",
            {
                "data": cfd,
                "chart_height": 320,
                "contract": view.template_context(),
            },
        )

    def _dashboard(request: Request, workflow_id: str) -> HTMLResponse:
        # Dashboard returns immediately — no chart data computed
        # here. Each tile lazy-loads via HTMX
        # `/api/internal/dashboard-tile/{metric}` so the page
        # paints fast and tiles fill in as their server-side
        # renders complete.
        view = _open_view(workflow_id, request)
        return templates.TemplateResponse(
            request,
            "dashboard.html.jinja",
            {
                "title": f"flowmetrics — {workflow_id}",
                "contract": view.template_context(),
                "available_contracts": _available_contracts(),
                "view": {"since": None, "until": None},
            },
        )

    @app.get(
        "/api/internal/dashboard-tile/{metric}",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def dashboard_tile(
        request: Request, metric: str, workflow: str,
    ) -> HTMLResponse:
        """HTMX-served per-metric tile (headline + chart). The
        dashboard renders only stubs; each tile fetches itself.
        The query string carries `view_from/to` + `ref_from/to`
        so the windows propagate to the tile's renders."""
        view = _open_view(workflow, request)
        today_utc = datetime.now(UTC).date()
        ctx: dict = {"contract": view.template_context()}
        with view.warehouse() as con:
            if metric == "cycle-time":
                ctx["data"] = view.render_cycle_time(con)
                tpl = "_partials/dashboard_tile_cycle_time.html.jinja"
            elif metric == "throughput":
                ctx["data"] = view.render_throughput(con)
                tpl = "_partials/dashboard_tile_throughput.html.jinja"
            elif metric == "aging":
                ctx["data"] = view.render_aging(con)
                tpl = "_partials/dashboard_tile_aging.html.jinja"
            elif metric == "cfd":
                ctx["data"] = view.render_cfd(con)
                tpl = "_partials/dashboard_tile_cfd.html.jinja"
            elif metric == "forecast":
                ctx["forecast_when_done"] = view.render_forecast_when_done(
                    con, items=20, start_date=today_utc,
                )
                ctx["forecast_how_many"] = view.render_forecast_how_many(
                    con,
                    start_date=today_utc,
                    end_date=today_utc + timedelta(days=6),
                )
                tpl = "_partials/dashboard_tile_forecast.html.jinja"
            else:
                raise HTTPException(
                    status_code=404,
                    detail=f"unknown metric {metric!r}",
                )
        return templates.TemplateResponse(request, tpl, ctx)

    # Two route declarations both delegating to `_dashboard`.
    # `/workflows/{id}` is the minimum URL; `/workflows/{id}/{slug}`
    # accepts a decorative slug for shareable canonical URLs. The
    # slug isn't used by routing — both forms render the same page.
    @app.get(
        "/workflows/{workflow_id}",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def dashboard(request: Request, workflow_id: str) -> HTMLResponse:
        return _dashboard(request, workflow_id)

    @app.get(
        "/workflows/{workflow_id}/{slug}",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def dashboard_with_slug(
        request: Request, workflow_id: str, slug: str
    ) -> HTMLResponse:
        return _dashboard(request, workflow_id)

    @app.get(
        "/workflows/{workflow_id}/metrics/cycle-time",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def cycle_time_detail(request: Request, workflow_id: str) -> HTMLResponse:
        view = _open_view(workflow_id, request)
        with view.warehouse() as con:
            cycle_time = view.render_cycle_time(con)
            work_items = render_work_items_table(con, workflow_id)
        return templates.TemplateResponse(
            request,
            "cycle_time_detail.html.jinja",
            {
                "title": f"Cycle time — {workflow_id}",
                # `metric_name` puts the metric in the site header
                # so detail pages identify themselves at the top of
                # the viewport. Dashboard routes pass no
                # `metric_name` — the header stays multi-metric-neutral.
                "metric_name": "Cycle time",
                "contract": view.template_context(),
                "available_contracts": _available_contracts(),
                "view": {"since": None, "until": None},
                "cycle_time": cycle_time,
                "work_items": work_items,
            },
        )

    # HTMX fragment endpoints — return JUST a chart's container +
    # script, no surrounding page chrome. Used by the reset button
    # on each chart tile (hx-get → outerHTML swap).
    @app.get(
        "/api/internal/cycle-time",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def cycle_time_fragment(
        request: Request, workflow: str
    ) -> HTMLResponse:
        view = _open_view(workflow, request)
        with view.warehouse() as con:
            cycle_time = view.render_cycle_time(con)
        return templates.TemplateResponse(
            request,
            "_partials/cycle_time_chart_fragment.html.jinja",
            {
                "data": cycle_time,
                "chart_height": 320,
                # Templates that the fragment includes (the chart
                # partial reads `contract.name` for href/hx-get URLs).
                "contract": view.template_context(),
            },
        )

    @app.get(
        "/workflows/{workflow_id}/metrics/throughput",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def throughput_detail(request: Request, workflow_id: str) -> HTMLResponse:
        view = _open_view(workflow_id, request)
        with view.warehouse() as con:
            throughput = view.render_throughput(con)
            work_items = render_work_items_table(con, workflow_id)
        return templates.TemplateResponse(
            request,
            "throughput_detail.html.jinja",
            {
                "title": f"Throughput — {workflow_id}",
                "metric_name": "Throughput",
                "contract": view.template_context(),
                "available_contracts": _available_contracts(),
                "view": {"since": None, "until": None},
                "throughput": throughput,
                "work_items": work_items,
            },
        )

    @app.get(
        "/api/internal/throughput",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def throughput_fragment(
        request: Request, workflow: str
    ) -> HTMLResponse:
        view = _open_view(workflow, request)
        with view.warehouse() as con:
            throughput = view.render_throughput(con)
        return templates.TemplateResponse(
            request,
            "_partials/throughput_chart_fragment.html.jinja",
            {
                "data": throughput,
                "chart_height": 280,
                "contract": view.template_context(),
            },
        )

    @app.get(
        "/workflows/{workflow_id}/metrics/aging",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def aging_detail(
        request: Request,
        workflow_id: str,
        asof: str | None = None,
    ) -> HTMLResponse:
        view = _open_view(workflow_id, request)
        asof_date = _parse_asof(asof)
        with view.warehouse() as con:
            aging = view.render_aging(con, asof=asof_date)
            # Table scope mirrors the chart: in-flight items only,
            # at the same asof. Completed items aren't part of
            # aging-WIP and don't belong here. Sort by start date
            # ascending so the oldest open item — the one most
            # likely past P85 — is at the top.
            work_items = render_work_items_table(
                con,
                workflow_id,
                in_flight_at=aging.asof_iso,
                sort="created_at",
                direction="asc",
                wip_states=(
                    view.contract.states.wip
                    if view.contract.states else None
                ),
            )
        return templates.TemplateResponse(
            request,
            "aging_detail.html.jinja",
            {
                "title": f"Aging WIP — {workflow_id}",
                "metric_name": "Aging WIP",
                "contract": view.template_context(),
                "available_contracts": _available_contracts(),
                "view": {"since": None, "until": None},
                "aging": aging,
                "work_items": work_items,
            },
        )

    @app.get(
        "/workflows/{workflow_id}/metrics/forecast",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def forecast_detail(
        request: Request,
        workflow_id: str,
        items: int = 20,
        days: int = 30,
    ) -> HTMLResponse:
        """Forecast page with two MCS panels driven by sliders.
        Initial render uses sane defaults (20 items, 30 days);
        sliders re-fetch the fragments via HTMX as the user drags."""
        view = _open_view(workflow_id, request)
        today = datetime.now(UTC).date()
        with view.warehouse() as con:
            when_done = render_forecast_when_done(
                con, workflow_id, items=max(1, items), start_date=today
            )
            # Slider semantics: "N days" → N-day inclusive window.
            # end_date = start + (N - 1) gives a window of size N.
            how_many = render_forecast_how_many(
                con,
                workflow_id,
                start_date=today,
                end_date=today + timedelta(days=max(0, days - 1)),
            )
        return templates.TemplateResponse(
            request,
            "forecast_detail.html.jinja",
            {
                "title": f"Forecast — {workflow_id}",
                "metric_name": "Forecast",
                "contract": view.template_context(),
                "available_contracts": _available_contracts(),
                "view": {"since": None, "until": None},
                "when_done": when_done,
                "how_many": how_many,
                "items_slider": items,
                "days_slider": days,
                "today_iso": today.isoformat(),
            },
        )

    @app.get(
        "/api/internal/forecast/when-done",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def forecast_when_done_fragment(
        request: Request,
        workflow: str,
        items: int = 20,
        start: str | None = None,
    ) -> HTMLResponse:
        view = _open_view(workflow, request)
        start_date = _parse_asof(start) or datetime.now(UTC).date()
        try:
            items_int = max(1, int(items))
        except (TypeError, ValueError):
            items_int = 20
        with view.warehouse() as con:
            data = render_forecast_when_done(
                con, workflow, items=items_int, start_date=start_date
            )
        return templates.TemplateResponse(
            request,
            "_partials/forecast_when_done_chart_fragment.html.jinja",
            {
                "data": data,
                "contract": view.template_context(),
                "chart_height": 280,
            },
        )

    @app.get(
        "/api/internal/forecast/how-many",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def forecast_how_many_fragment(
        request: Request,
        workflow: str,
        days: int = 30,
        start: str | None = None,
    ) -> HTMLResponse:
        view = _open_view(workflow, request)
        start_date = _parse_asof(start) or datetime.now(UTC).date()
        try:
            days_int = max(1, int(days))
        except (TypeError, ValueError):
            days_int = 30
        with view.warehouse() as con:
            data = render_forecast_how_many(
                con,
                workflow,
                start_date=start_date,
                # Same N → N-day window semantics as the detail page.
                end_date=start_date + timedelta(days=max(0, days_int - 1)),
            )
        return templates.TemplateResponse(
            request,
            "_partials/forecast_how_many_chart_fragment.html.jinja",
            {
                "data": data,
                "contract": view.template_context(),
                "chart_height": 280,
            },
        )

    @app.post(
        "/api/internal/materialise",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def materialise_action(
        request: Request,
        workflow: str,
        since: str | None = None,
        until: str | None = None,
        asof: str | None = None,
    ) -> HTMLResponse:
        """Trigger `flow materialise` from the UI (the button on
        the aging empty-state) and return the refreshed aging chart
        fragment so HTMX can swap it in place.

        Synchronous: runs the materialise inline, holds the request
        until done. Fine for small contracts (seconds). A future
        async + job-polling version can replace this without
        changing the UI contract (button POSTs → fragment returns).
        """
        import dataclasses

        from .materialise import materialise as run_materialise

        view = _open_view(workflow, request)
        contract_obj = view.contract

        since_date = _parse_asof(since)
        until_date = _parse_asof(until)
        overrides: dict = {}
        if since_date is not None:
            overrides["start"] = since_date
        if until_date is not None:
            overrides["stop"] = until_date
        if overrides:
            contract_obj = dataclasses.replace(contract_obj, **overrides)

        try:
            run_materialise(
                contract=contract_obj,
                data_dir=data_dir,
                cache_dir=cache_dir,
                offline=False,  # button → hit the source, fetch new
            )
        except Exception as exc:  # noqa: BLE001 — surface anything
            # Re-render the empty state with the error message so
            # the operator sees what went wrong without leaving the
            # page. Wraps the original aging payload (which still
            # has its coverage/asof info) in an "import failed"
            # banner.
            asof_date = _parse_asof(asof)
            with view.warehouse() as con:
                aging = view.render_aging(con, asof=asof_date)
            return templates.TemplateResponse(
                request,
                "_partials/aging_chart_fragment.html.jinja",
                {
                    "data": aging,
                    "chart_height": 320,
                    "contract": view.template_context(),
                    "materialise_error": str(exc),
                },
            )

        # Success: re-render the aging fragment against the fresh
        # warehouse. HTMX swaps it in place.
        asof_date = _parse_asof(asof)
        with view.warehouse() as con:
            aging = view.render_aging(con, asof=asof_date)
        return templates.TemplateResponse(
            request,
            "_partials/aging_chart_fragment.html.jinja",
            {
                "data": aging,
                "chart_height": 320,
                "contract": view.template_context(),
            },
        )

    @app.get(
        "/api/internal/aging",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def aging_fragment(
        request: Request, workflow: str, asof: str | None = None
    ) -> HTMLResponse:
        view = _open_view(workflow, request)
        asof_date = _parse_asof(asof)
        with view.warehouse() as con:
            aging = view.render_aging(con, asof=asof_date)
        return templates.TemplateResponse(
            request,
            "_partials/aging_chart_fragment.html.jinja",
            {
                "data": aging,
                "chart_height": 320,
                "contract": view.template_context(),
            },
        )

    @app.get(
        "/workflows/{workflow_id}/items/{item_id:path}",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def item_lifecycle(
        request: Request,
        workflow_id: str,
        item_id: str,
    ) -> HTMLResponse:
        """Per-item lifecycle: timeline of stage transitions for one
        work item, plus a tabular event list below.

        The contract specifies the source (github / jira) — we don't
        repeat it in the URL since it's an implementation detail of
        where the data came from, not part of the resource identity.

        `item_id` uses `:path` so GitHub ids like `#19342` (URL-
        encoded as `%23…`) and Jira keys like `ABC-123` both pass
        through cleanly without further matching tricks. We strip
        the GitHub-specific `#` if the caller omits it — the
        warehouse stores items with `#` for GitHub PR numbers
        per the source adapter convention, but the URL accepts
        either form.
        """
        view = _open_view(workflow_id, request)
        # Accept ids with or without the leading `#` for GitHub
        # PR numbers — both `12345` and `#12345` route to the
        # canonical warehouse id (`#12345`).
        resolved_id = item_id
        if view.contract.source == "github" and not item_id.startswith("#"):
            resolved_id = "#" + item_id
        try:
            with view.warehouse() as con:
                data = view.render_lifecycle(
                    con,
                    source=view.contract.source,
                    item_id=resolved_id,
                )
        except ItemNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "item_lifecycle.html.jinja",
            {
                "title": f"{data.item_id} — {workflow_id}",
                # The item id goes in the site header so the page
                # identifies itself at the top of the viewport.
                "metric_name": data.item_id,
                "contract": view.template_context(),
                "available_contracts": _available_contracts(),
                "view": {"since": None, "until": None},
                "data": data,
            },
        )

    @app.get(
        "/api/internal/work-items",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def work_items_fragment(
        request: Request,
        workflow: str,
        q: str | None = None,
        completed_on: str | None = None,
        in_flight_at: str | None = None,
        sort: str = "completed_at",
        direction: str = "desc",
        page: int = 1,
    ) -> HTMLResponse:
        """HTMX swap target for the work-items table: sort + filter
        + paginate round-trip, returning only the partial.

        `completed_on` drills the table to a single completion date
        (used by the throughput chart's bar-click handler).
        `in_flight_at` restricts to items in-flight at the given
        UTC date — the aging detail page passes this; pagination
        and sort links propagate it so clicking "Next" doesn't
        silently drop the in-flight scope.
        `page` is 1-indexed; the component clamps to the valid
        range and caps page_size at 200.
        """
        view = _open_view(workflow, request)
        # Normalise sort/direction via the component's whitelist —
        # invalid values fall back to defaults inside render().
        sort_key: SortKey = sort if sort in (
            "item_id", "title", "created_at",
            "completed_at", "cycle_time_days", "age_days",
        ) else "completed_at"
        direction_key: SortDir = direction if direction in (
            "asc", "desc"
        ) else "desc"
        try:
            page_int = max(1, int(page))
        except (TypeError, ValueError):
            page_int = 1
        # On the aging page, the route sorts in-flight items by
        # created_at ASC (oldest open first). HTMX-driven pagination
        # without an explicit sort override should keep that
        # contract.
        if in_flight_at and sort == "completed_at":
            sort_key = "created_at"
            direction_key = "asc"
        with view.warehouse() as con:
            data = render_work_items_table(
                con,
                workflow,
                q=q,
                completed_on=completed_on,
                in_flight_at=in_flight_at,
                sort=sort_key,
                direction=direction_key,
                page=page_int,
                wip_states=(
                    view.contract.states.wip
                    if view.contract.states else None
                ),
            )
        return templates.TemplateResponse(
            request,
            # Return only the BODY partial — the search input lives
            # outside the swap target on the page (see
            # work_items_table.html.jinja). HTMX swaps innerHTML of
            # #work-items-body, keeping the input's DOM identity
            # (focus, caret, IME composition) intact across requests.
            "_partials/work_items_table_body.html.jinja",
            {
                "data": data,
                "contract": view.template_context(),
            },
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return app
