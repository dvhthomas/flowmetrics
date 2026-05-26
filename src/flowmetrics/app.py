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

import os
import secrets
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import duckdb
from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .backfill import (
    display_status,
    is_active,
    read_status,
    status_path,
    write_status,
)
from .contract import (
    ContractError,
    load_contract,
    parse_contract_text,
    validate_yaml_text_structured,
)
from .utc_dates import to_utc_display_date
from .warehouse.connection import open_warehouse
from .warehouse.queries import completion_date_range, latest_materialised_at
from .web.components.aging import render as render_aging
from .web.components.cfd import render as render_cfd
from .web.components.cycle_time import render as render_cycle_time
from .web.components.data_source import render as render_data_source
from .web.components.forecast import (
    render_how_many as render_forecast_how_many,
)
from .web.components.forecast import (
    render_when_done as render_forecast_when_done,
)
from .web.components.lifecycle import (
    ItemNotFound,
)
from .web.components.lifecycle import (
    render as render_lifecycle,
)
from .web.components.throughput import render as render_throughput
from .web.components.work_items_table import (
    SortDir,
    SortKey,
)
from .web.components.work_items_table import (
    render as render_work_items_table,
)
from .windows import parse_windows

_TEMPLATES_DIR = Path(__file__).parent / "web" / "templates"


# `subprocess.CREATE_NEW_PROCESS_GROUP` is the Win32 flag value
# 0x00000200 — only attached to the `subprocess` module on Windows.
# We hard-code the constant so the helper is testable on POSIX too;
# the call site only passes it through `creationflags=` when running
# on Windows.
_WIN_CREATE_NEW_PROCESS_GROUP = 0x00000200


def _detached_popen_kwargs(os_name: str | None = None) -> dict[str, object]:
    """OS-appropriate kwargs for spawning a detached child that
    outlives the request worker. POSIX uses `start_new_session`;
    Windows wants `CREATE_NEW_PROCESS_GROUP` — passing the POSIX
    flag on Windows raises. Unknown OS → no extras (caller still
    gets a working but non-detached child)."""
    name = os.name if os_name is None else os_name
    if name == "posix":
        return {"start_new_session": True}
    if name == "nt":
        return {"creationflags": _WIN_CREATE_NEW_PROCESS_GROUP}
    return {}

# Source `kind` → human label for the snapshot-section aside on
# the dashboard ("most recent GitHub import"). New backends added
# here; `.title()` is a safe fallback.
_SOURCE_DISPLAY = {"github": "GitHub", "jira": "Jira"}


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
        today_utc = datetime.now(UTC).date()
        # The completion-data coverage. `data_max_date` anchors
        # the reference period; both bound the filter-bar date
        # inputs so the user can't pick a Period Ending with no
        # data behind it.
        self.data_min_date, self.data_max_date = (
            self._completion_date_range()
        )
        # `parse_windows` is the single place date math happens —
        # the filter bar only emits a `period` choice. It returns
        # the WindowSelection model that every view and the filter
        # bar read; nothing downstream re-decides dates. The view
        # anchor defaults to today and is honoured verbatim (a
        # stale workflow loads on today's empty window; the NODATA
        # state explains it). The reference is anchored to
        # `data_max`, independent of the view.
        self.today = today_utc
        self.selection = parse_windows(
            dict(query or {}),
            today=today_utc,
            data_max=self.data_max_date,
            data_min=self.data_min_date,
        )
        # "Data is stale" diagnostic for the template. True when
        # the warehouse's latest completion is meaningfully
        # before today (>= 2 days) — typical cron lag is one
        # day, so two-day staleness is the noise floor.
        self.data_is_stale = (
            self.data_max_date is not None
            and (today_utc - self.data_max_date).days >= 2
        )

    def _completion_date_range(self):
        """`(earliest, latest)` completed_at dates in this
        workflow's warehouse — the coverage the filter-bar date
        inputs are bounded to. `(None, None)` when the warehouse
        has no completions yet."""
        try:
            con = open_warehouse(self._data_dir)
        except duckdb.IOException:
            return None, None
        try:
            return completion_date_range(con, self.id)
        finally:
            con.close()

    def _aging_snapshot_date(self) -> date | None:
        """The aging snapshot's asof — the date of the latest
        materialise for this workflow. `None` when the warehouse
        has no rows yet. The Aging tile pins itself to this
        moment; the dashboard's snapshot-section header names it."""
        try:
            con = open_warehouse(self._data_dir)
        except duckdb.IOException:
            return None
        try:
            return latest_materialised_at(con, self.id)
        finally:
            con.close()

    @property
    def view_window(self):
        """The view window — accessor for `self.selection.view`.
        The model is `self.selection`; this is just ergonomics."""
        return self.selection.view

    @property
    def reference_period(self):
        """The reference period — accessor for the model's
        `self.selection.reference`."""
        return self.selection.reference

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
        """Context for the filter bar + breadcrumb.

        `window` is the `WindowSelection` model — the filter bar
        reads its `period` / `anchor` / `view_days` / `ref_days`
        / `is_advanced` directly, no flattening. The rest is
        workflow identity + the data-coverage bounds for the
        Period Ending date input.
        """
        return {
            "name": self.id,
            # Human-friendly display name. Templates render
            # `contract.label` in breadcrumbs / home-page lists;
            # `contract.name` stays the URL-safe routing ID.
            "label": self.contract.label or self.id,
            "slug": self._slug(),
            # The one filter model — see flowmetrics.windows.
            "window": self.selection,
            # The reference sample follows the Period by default,
            # so the "default" ref length is the current view
            # length — the filter bar strips ref_days from the URL
            # when it equals this.
            "default_ref_days": self.selection.view_days,
            # Completion-data coverage. Bounds the Period Ending
            # date input (min/max) and drives the freshness
            # banner. None when the warehouse is empty.
            "data_min_date": (
                self.data_min_date.isoformat()
                if self.data_min_date else None
            ),
            "data_max_date": (
                self.data_max_date.isoformat()
                if self.data_max_date else None
            ),
            "data_is_stale": self.data_is_stale,
            # The aging snapshot's asof — the date the dashboard's
            # Snapshot-section header names. Aging WIP is pinned to
            # this moment regardless of the Period picker.
            "aging_asof_display": self._aging_asof_display(),
            # Source display name for the snapshot-section aside
            # ("most recent GitHub import" / "Jira import").
            "source_display": _SOURCE_DISPLAY.get(
                self.contract.source, self.contract.source.title()
            ),
        }

    def _aging_asof_display(self) -> str | None:
        asof = self._aging_snapshot_date()
        if asof is None:
            return None
        return to_utc_display_date(
            datetime.combine(asof, datetime.min.time(), tzinfo=UTC)
        )

    def _slug(self) -> str:
        c = self.contract
        if c.source == "github" and c.repo:
            return c.repo.lower().replace("/", "-").replace(".", "-")
        if c.source == "jira" and c.jira_project:
            return c.jira_project.lower()
        return self.id

    # ---- render orchestration ------------------------------------

    def render_cfd(self, con):
        # The Period is a VISUAL window on the CFD — it clamps the
        # x-axis. The cumulative math stays full-history, so the
        # carry-in at the window's left edge is correct; the
        # y-axis floor slider crops that inert base.
        return render_cfd(
            con, self.id,
            states=self.contract.states,
            view=self.view_window,
        )

    def render_aging(self, con):
        # Aging WIP is a "right now" snapshot — pinned to the
        # in-flight snapshot date (the latest materialise), NOT
        # the Period anchor. The warehouse holds one in-flight
        # snapshot, so aging can only be faithfully computed at
        # that date.
        asof = latest_materialised_at(con, self.id) or self.today
        return render_aging(
            con, self.id,
            asof=asof,
            states=self.contract.states,
            reference=self.selection.reference,
        )

    def render_cycle_time(self, con):
        return render_cycle_time(
            con, self.id,
            # Cycle-time's percentile lines summarise the dots on
            # screen — so they sample the view window, not the
            # reference. The reference is for aging + forecasts.
            view=self.selection.view,
        )

    def render_throughput(self, con):
        # The throughput model derives its own coverage from the
        # observed completion span — caller passes only the view.
        return render_throughput(con, self.id, view=self.view_window)

    def render_forecast_when_done(self, con, *, items: int, start_date=None):
        return render_forecast_when_done(
            con, self.id,
            items=items,
            # A forecast runs from "now" by default — `self.today`,
            # the model's clock, not an ad-hoc now() in a route.
            start_date=start_date or self.today,
            reference=self.selection.reference,
        )

    def render_forecast_how_many(self, con, *, start_date=None, end_date=None):
        start = start_date or self.today
        return render_forecast_how_many(
            con, self.id,
            start_date=start,
            # Default horizon: 7 inclusive days from the start.
            end_date=end_date or (start + timedelta(days=6)),
            reference=self.selection.reference,
        )

    def render_lifecycle(self, con, *, source: str, item_id: str):
        return render_lifecycle(con, self.id, source, item_id)


def create_app(
    *,
    data_dir: Path,
    contracts_dir: Path,
    cache_dir: Path | None = None,
    password: str | None = None,
    offline: bool = False,
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
    from urllib.parse import parse_qsl, urlencode

    from jinja2 import pass_context

    _CARRIED_PARAMS = frozenset({
        "period", "anchor", "view_days", "ref_days",
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

    # `vega_spec` Jinja global — turns a chart model into its
    # Vega-Lite spec JSON. The chart fragment templates call
    # `{{ vega_spec(data) | safe }}`; `to_vega` dispatches on the
    # model type, so each migrated chart registers its own
    # translator (see web/components/_vega.py).
    from .web.components._vega import vega_spec_json

    templates.env.globals["vega_spec"] = vega_spec_json

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

    def _parse_iso_date(value: str | None):
        """`YYYY-MM-DD` query param → `date`. `None` or invalid →
        `None` (the caller supplies its own default).
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
                "first."
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
        # Both extensions — `load_contract` accepts either, so the
        # listing should too. A duplicate `foo.yaml` + `foo.yml`
        # collapses to a single id (load_contract picks .yaml first).
        seen: set[str] = set()
        for ext in ("*.yaml", "*.yml"):
            for p in contracts_dir.glob(ext):
                seen.add(p.stem)
        return sorted(seen)

    def _raw_yaml_text(name: str) -> str | None:
        """Best-effort read of the YAML file as-is. Used by the
        contract-detail API so the UI can show + edit the original
        text alongside the parsed view. Returns None when the file
        is unreadable (caller decides what to do)."""
        for ext in (".yaml", ".yml"):
            p = contracts_dir / f"{name}{ext}"
            if p.exists():
                try:
                    return p.read_text()
                except OSError:
                    return None
        return None

    def _materialise_status(name: str) -> dict | None:
        """Latest known per-workflow materialise outcome. Reads the
        status file the browser-backfill flow writes; returns None
        if no run has happened yet."""
        spath = status_path(data_dir, name)
        rec = read_status(spath)
        if rec is None:
            return None
        return {
            "last_run_at": rec.get("finished_at") or rec.get("started_at"),
            "status": rec.get("status"),
            "items": rec.get("items"),
            "message": rec.get("message"),
        }

    def _contract_summary(name: str) -> dict:
        """List-row payload. Falls back to bare id+source when the
        YAML fails to parse so the listing still tells the user
        what's on disk — the detail endpoint surfaces the error."""
        try:
            c = load_contract(name, contracts_dir)
            return {
                "id": c.name,
                "label": c.label or c.name,
                "source": c.source,
            }
        except ContractError:
            return {"id": name, "label": name, "source": None}

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
                # Surface the directory we actually scanned —
                # operators routinely run `flow serve` from a place
                # where the default `./contracts` is empty/missing
                # and need to see the resolved path to debug.
                "contracts_dir_display": str(contracts_dir.resolve()),
                # Empty contract keeps `_base.html.jinja` header
                # / filter-bar guards happy without implying a
                # current workflow.
                "contract": None,
            },
        )

    # ------------------------------------------------------------------
    # Contract management API (B1+). Read endpoints first; the wizard
    # UI (B3..B5) layers on top.
    # ------------------------------------------------------------------

    def _require_csrf(request: Request) -> None:
        """Drive-by cross-origin POST/PUT/DELETE can't set custom
        request headers without triggering a CORS preflight (which
        this app doesn't accept). Requiring X-Requested-With: fetch
        on writes is enough proof the request came from our own UI."""
        if request.headers.get("X-Requested-With") != "fetch":
            raise HTTPException(
                status_code=403,
                detail=(
                    "writes require the X-Requested-With: fetch header. "
                    "This blocks drive-by cross-origin POSTs from "
                    "other tabs."
                ),
            )

    write_dep = [Depends(_require_csrf), *auth_dep]

    @app.get("/api/internal/contracts", dependencies=auth_dep)
    def list_contracts() -> list[dict]:
        """List every workflow YAML under contracts_dir.

        Tolerates malformed YAML — broken entries still appear in the
        list (with source=null) so the user can find and fix them
        through the UI; the detail endpoint surfaces the parser error.
        """
        return [_contract_summary(name) for name in _available_contracts()]

    @app.get("/api/internal/contracts/{contract_id}", dependencies=auth_dep)
    def get_contract(contract_id: str) -> dict:
        """Full detail for one contract: parsed dataclass fields,
        the original YAML text, and the most recent materialise
        status. 404 if the file is missing; 422 if it's malformed."""
        _ensure_contract_exists(contract_id)
        raw = _raw_yaml_text(contract_id)
        try:
            c = load_contract(contract_id, contracts_dir)
        except ContractError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"contract {contract_id!r} failed to parse: {exc}",
            ) from exc
        parsed: dict = {
            "name": c.name,
            "source": c.source,
            "repo": c.repo,
            "jira_url": c.jira_url,
            "jira_project": c.jira_project,
            "start": c.start.isoformat() if c.start else None,
            "stop": c.stop.isoformat() if c.stop else None,
            "label": c.label,
        }
        if c.states is not None:
            parsed["states"] = {
                "backlog": list(c.states.backlog),
                "wip": list(c.states.wip),
                "done": list(c.states.done),
            }
        return {
            "id": c.name,
            "label": c.label or c.name,
            "parsed": parsed,
            "yaml": raw or "",
            "materialise": _materialise_status(contract_id),
        }

    @app.post(
        "/api/internal/contracts/_validate", dependencies=write_dep,
    )
    def validate_contract(payload: dict) -> dict:
        """Validate a YAML body without touching disk. Always returns
        200; the response carries `{valid, errors}` so the UI can
        render line-level feedback inline."""
        text = payload.get("yaml") or ""
        errors = validate_yaml_text_structured(text)
        return {"valid": not errors, "errors": errors}

    @app.put(
        "/api/internal/contracts/{contract_id}", dependencies=write_dep,
    )
    def put_contract(contract_id: str, payload: dict) -> dict:
        """Create or overwrite a contract YAML atomically. Body must
        carry a `yaml` STRING that parses against `parse_contract_text`
        with `name == contract_id`."""
        text = payload.get("yaml") or ""
        try:
            parse_contract_text(text, contract_id)
        except ContractError as exc:
            errors = validate_yaml_text_structured(text, contract_id)
            raise HTTPException(
                status_code=422,
                detail={"message": str(exc), "errors": errors},
            ) from exc

        # Atomic write: tmp → rename. tmp lives in the same dir so
        # the rename stays on one filesystem.
        target = contracts_dir / f"{contract_id}.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".yaml.tmp")
        tmp.write_text(text)
        os.replace(tmp, target)
        return get_contract(contract_id)

    @app.delete(
        "/api/internal/contracts/{contract_id}", dependencies=write_dep,
    )
    async def delete_contract(
        contract_id: str, request: Request,
    ) -> dict:
        """Remove a contract YAML. Refuses if Parquet exists for that
        contract unless `?purge_data=true` (or a JSON body
        `{"purge_data": true}`) is passed — the flag also wipes
        `<data-dir>/work_items/contract_id=<id>` and the matching
        `transitions/` + `runs/` directories."""
        _ensure_contract_exists(contract_id)

        # Accept the flag from EITHER the query string (the standard
        # DELETE-with-no-body shape) OR a JSON body (what fetch()
        # callers naturally send). Whichever the UI chooses works.
        purge = (request.query_params.get("purge_data") or "").lower() in (
            "true", "1", "yes",
        )
        if not purge:
            try:
                raw = await request.body()
                if raw:
                    import json as _json
                    purge = bool(_json.loads(raw).get("purge_data"))
            except (ValueError, TypeError):
                purge = False

        partition_dirs = [
            data_dir / "work_items" / f"contract_id={contract_id}",
            data_dir / "transitions" / f"contract_id={contract_id}",
            data_dir / "runs" / contract_id,
        ]
        has_data = any(p.exists() and any(p.rglob("*")) for p in partition_dirs)
        if has_data and not purge:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"contract {contract_id!r} has warehouse data on "
                    "disk. Pass `{\"purge_data\": true}` to delete "
                    "the YAML AND the partitioned Parquet, or run "
                    "`flow restore` first if you wanted to keep it."
                ),
            )

        for ext in (".yaml", ".yml"):
            p = contracts_dir / f"{contract_id}{ext}"
            if p.exists():
                p.unlink()
        if purge:
            import shutil

            for d in partition_dirs:
                if d.exists():
                    shutil.rmtree(d)
        return {"id": contract_id, "deleted": True, "purged": purge}

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

    # ---- Data Source page: coverage + browser-driven backfill ----

    @app.get(
        "/workflows/{workflow_id}/data-source",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def data_source_detail(
        request: Request, workflow_id: str,
    ) -> HTMLResponse:
        """Per-workflow Data Source page: a completion-coverage
        timeline plus a browser-driven backfill the operator runs
        without ever touching the `flow materialise` CLI."""
        view = _open_view(workflow_id, request)
        with view.warehouse() as con:
            coverage = render_data_source(con, workflow_id)
        return templates.TemplateResponse(
            request,
            "data_source.html.jinja",
            {
                "title": f"Data Source — {workflow_id}",
                "metric_name": "Data Source",
                "contract": view.template_context(),
                "available_contracts": _available_contracts(),
                "view": {"since": None, "until": None},
                "workflow": workflow_id,
                "coverage": coverage,
                "backfill": display_status(
                    read_status(status_path(data_dir, workflow_id)),
                    datetime.now(UTC),
                ),
                "today": view.today.isoformat(),
            },
        )

    @app.post(
        "/api/internal/backfill",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def backfill_start(
        request: Request,
        workflow: str = Form(...),
        since: str = Form(...),
        until: str = Form(...),
    ) -> HTMLResponse:
        """Kick off a backfill: spawn a detached `flow materialise`
        subprocess that writes a JSON status file the page polls.
        The status file is the lock — one backfill per workflow."""
        _ensure_contract_exists(workflow)
        spath = status_path(data_dir, workflow)

        def _fragment(status: dict | None) -> HTMLResponse:
            return templates.TemplateResponse(
                request,
                "_partials/backfill_progress.html.jinja",
                {"backfill": status, "workflow": workflow},
            )

        existing = read_status(spath)
        if is_active(existing, datetime.now(UTC)):
            # A backfill is genuinely in flight — don't double-spawn.
            # (A stale "running" record from a crashed run is not
            # active, so it never wedges the workflow.)
            return _fragment(existing)
        try:
            date.fromisoformat(since)
            date.fromisoformat(until)
        except ValueError:
            return _fragment({
                "workflow": workflow, "since": since, "until": until,
                "status": "failed", "message": "Invalid date range.",
            })
        # Mark running now — closes the race where the page polls
        # before the subprocess writes its own first record.
        write_status(spath, {
            "workflow": workflow, "since": since, "until": until,
            "status": "running",
            "started_at": datetime.now(UTC).isoformat(),
            "finished_at": None, "message": "",
        })
        cmd = [
            sys.executable, "-m", "flowmetrics", "materialise", workflow,
            "--data-dir", str(data_dir),
            "--workflows-dir", str(contracts_dir),
            "--cache-dir", str(cache_dir),
            "--since", since, "--until", until,
            "--status-file", str(spath),
        ]
        if offline:
            cmd.append("--offline")
        # Detached: survives this request worker. The detach flag
        # itself is OS-conditional (POSIX `start_new_session` vs.
        # Windows `creationflags=CREATE_NEW_PROCESS_GROUP`); see
        # `_detached_popen_kwargs`.
        subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_detached_popen_kwargs(),
        )
        return _fragment(read_status(spath))

    @app.get(
        "/api/internal/backfill-status",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def backfill_status(
        request: Request, workflow: str,
    ) -> HTMLResponse:
        """Polled by the progress fragment while a backfill runs."""
        return templates.TemplateResponse(
            request,
            "_partials/backfill_progress.html.jinja",
            {
                "backfill": display_status(
                    read_status(status_path(data_dir, workflow)),
                    datetime.now(UTC),
                ),
                "workflow": workflow,
                # Poll-delivered: a `done` fragment from here means
                # the backfill just finished, so the fragment
                # refreshes the page. The page-load + POST renders
                # omit this, so they never trigger a reload loop.
                "poll": True,
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
        The query string carries the filter params (period /
        anchor / view_days / ref_days) so the selected window
        propagates to the tile's renders."""
        view = _open_view(workflow, request)
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
                # 20-item / 7-day preview; the forecast clock
                # (`start_date`) defaults to the model's today.
                ctx["forecast_when_done"] = view.render_forecast_when_done(
                    con, items=20,
                )
                ctx["forecast_how_many"] = view.render_forecast_how_many(con)
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
            work_items = render_work_items_table(
                con, workflow_id, view=view.view_window,
            )
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
            work_items = render_work_items_table(
                con, workflow_id, view=view.view_window,
            )
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
    ) -> HTMLResponse:
        view = _open_view(workflow_id, request)
        with view.warehouse() as con:
            aging = view.render_aging(con)
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
        # The forecast clock comes from the model, not an ad-hoc
        # now() in the route.
        today = view.today
        with view.warehouse() as con:
            # Go through the view's render methods so the reference
            # window (which follows the Period) reaches the MCS
            # sample — not the bare component, which would forecast
            # off the full history regardless of the Period.
            when_done = view.render_forecast_when_done(
                con, items=max(1, items), start_date=today
            )
            # Slider semantics: "N days" → N-day inclusive window.
            # end_date = start + (N - 1) gives a window of size N.
            how_many = view.render_forecast_how_many(
                con,
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
        start_date = _parse_iso_date(start) or view.today
        try:
            items_int = max(1, int(items))
        except (TypeError, ValueError):
            items_int = 20
        with view.warehouse() as con:
            data = view.render_forecast_when_done(
                con, items=items_int, start_date=start_date
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
        start_date = _parse_iso_date(start) or view.today
        try:
            days_int = max(1, int(days))
        except (TypeError, ValueError):
            days_int = 30
        with view.warehouse() as con:
            data = view.render_forecast_how_many(
                con,
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

    @app.get(
        "/api/internal/aging",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def aging_fragment(
        request: Request, workflow: str
    ) -> HTMLResponse:
        view = _open_view(workflow, request)
        with view.warehouse() as con:
            aging = view.render_aging(con)
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
                # View window applies in completed-items mode
                # (cycle-time / throughput pages); the component
                # auto-skips it when in_flight_at is set (aging).
                view=view.view_window,
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
