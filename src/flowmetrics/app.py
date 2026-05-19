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
from pathlib import Path

import duckdb
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates

from .web.components.aging import render as render_aging
from .web.components.cycle_time import render as render_cycle_time
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


def create_app(
    *,
    data_dir: Path,
    contracts_dir: Path,
    password: str | None = None,
) -> FastAPI:
    """Build a FastAPI app reading from this data dir.

    Each call returns an isolated app — multiple instances (one per
    contract scope / data directory) share zero state. Mounted as a
    subprocess of uvicorn, started by `flow serve`.

    `password` opt-in: when set, every request requires HTTP Basic
    auth (user='operator', password matches). Used for off-localhost
    binds where Tailscale or Caddy fronts the app.
    """
    app = FastAPI(title="flowmetrics", version="1")
    templates = Jinja2Templates(directory=str(_TEMPLATES_DIR))

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

    def _open_warehouse() -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(":memory:")
        # Register both fact-table views across all partitions for
        # this data_dir. The /**/ glob is intentionally agnostic to
        # partition depth (year=…/month=…/day=…); Hive partition keys
        # are discovered automatically. `transitions` may not exist
        # on a brand-new install — fall back to an empty view so
        # routes that don't need it still work.
        for kind in ("work_items", "transitions"):
            glob = (data_dir / kind / "**" / "*.parquet").as_posix()
            try:
                con.execute(
                    f"CREATE VIEW {kind} AS "
                    f"SELECT * FROM read_parquet('{glob}', "
                    f"hive_partitioning = true)"
                )
            except duckdb.IOException:
                # No parquet files yet for this fact table — register
                # an empty stub view with the right column shape so
                # downstream queries don't crash on missing relations.
                if kind == "transitions":
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
                else:
                    raise
        return con

    def _available_contracts() -> list[str]:
        return sorted(p.stem for p in contracts_dir.glob("*.yaml"))

    # `/` redirects to the first contract's dashboard. Once Slice 6
    # adds the contract switcher UI this becomes the contract picker;
    # for v1 it's just a convenience for single-contract installs.
    @app.get("/", include_in_schema=False)
    def root_redirect():
        from fastapi.responses import RedirectResponse

        return RedirectResponse(
            url=f"/contracts/{_first_contract_or_404()}/", status_code=307
        )

    @app.get(
        "/contracts/{contract_id}/",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def dashboard(request: Request, contract_id: str) -> HTMLResponse:
        _ensure_contract_exists(contract_id)
        # The dashboard is metric-overview only; the per-item table
        # belongs to the metric detail pages. Don't pay the cost of
        # building it here.
        with _open_warehouse() as con:
            cycle_time = render_cycle_time(con, contract_id)
            throughput = render_throughput(con, contract_id)
            aging = render_aging(con, contract_id)
        return templates.TemplateResponse(
            request,
            "dashboard.html.jinja",
            {
                "title": f"flowmetrics — {contract_id}",
                "contract": {"name": contract_id},
                "available_contracts": _available_contracts(),
                "view": {"since": None, "until": None},
                "cycle_time": cycle_time,
                "throughput": throughput,
                "aging": aging,
            },
        )

    @app.get(
        "/contracts/{contract_id}/metrics/cycle-time",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def cycle_time_detail(request: Request, contract_id: str) -> HTMLResponse:
        _ensure_contract_exists(contract_id)
        with _open_warehouse() as con:
            cycle_time = render_cycle_time(con, contract_id)
            work_items = render_work_items_table(con, contract_id)
        return templates.TemplateResponse(
            request,
            "cycle_time_detail.html.jinja",
            {
                "title": f"Cycle time — {contract_id}",
                # `metric_name` puts the metric in the site header
                # so detail pages identify themselves at the top of
                # the viewport. Dashboard routes pass no
                # `metric_name` — the header stays multi-metric-neutral.
                "metric_name": "Cycle time",
                "contract": {"name": contract_id},
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
    def cycle_time_fragment(request: Request, contract: str) -> HTMLResponse:
        _ensure_contract_exists(contract)
        with _open_warehouse() as con:
            cycle_time = render_cycle_time(con, contract)
        return templates.TemplateResponse(
            request,
            "_partials/cycle_time_chart_fragment.html.jinja",
            {
                "data": cycle_time,
                "chart_height": 320,
                # Templates that the fragment includes (the chart
                # partial reads `contract.name` for href/hx-get URLs).
                "contract": {"name": contract},
            },
        )

    @app.get(
        "/contracts/{contract_id}/metrics/throughput",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def throughput_detail(request: Request, contract_id: str) -> HTMLResponse:
        _ensure_contract_exists(contract_id)
        with _open_warehouse() as con:
            throughput = render_throughput(con, contract_id)
            work_items = render_work_items_table(con, contract_id)
        return templates.TemplateResponse(
            request,
            "throughput_detail.html.jinja",
            {
                "title": f"Throughput — {contract_id}",
                "metric_name": "Throughput",
                "contract": {"name": contract_id},
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
    def throughput_fragment(request: Request, contract: str) -> HTMLResponse:
        _ensure_contract_exists(contract)
        with _open_warehouse() as con:
            throughput = render_throughput(con, contract)
        return templates.TemplateResponse(
            request,
            "_partials/throughput_chart_fragment.html.jinja",
            {
                "data": throughput,
                "chart_height": 280,
                "contract": {"name": contract},
            },
        )

    @app.get(
        "/contracts/{contract_id}/metrics/aging",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def aging_detail(
        request: Request,
        contract_id: str,
        asof: str | None = None,
    ) -> HTMLResponse:
        _ensure_contract_exists(contract_id)
        asof_date = _parse_asof(asof)
        with _open_warehouse() as con:
            aging = render_aging(con, contract_id, asof=asof_date)
            work_items = render_work_items_table(con, contract_id)
        return templates.TemplateResponse(
            request,
            "aging_detail.html.jinja",
            {
                "title": f"Aging WIP — {contract_id}",
                "metric_name": "Aging WIP",
                "contract": {"name": contract_id},
                "available_contracts": _available_contracts(),
                "view": {"since": None, "until": None},
                "aging": aging,
                "work_items": work_items,
            },
        )

    @app.get(
        "/api/internal/aging",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def aging_fragment(
        request: Request, contract: str, asof: str | None = None
    ) -> HTMLResponse:
        _ensure_contract_exists(contract)
        asof_date = _parse_asof(asof)
        with _open_warehouse() as con:
            aging = render_aging(con, contract, asof=asof_date)
        return templates.TemplateResponse(
            request,
            "_partials/aging_chart_fragment.html.jinja",
            {
                "data": aging,
                "chart_height": 320,
                "contract": {"name": contract},
            },
        )

    @app.get(
        "/contracts/{contract_id}/items/{item_id:path}",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def item_lifecycle(
        request: Request,
        contract_id: str,
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
        _ensure_contract_exists(contract_id)
        from .contract import load_contract

        contract = load_contract(contract_id, contracts_dir)
        # Accept ids with or without the leading `#` for GitHub
        # PR numbers — both `12345` and `#12345` route to the
        # canonical warehouse id (`#12345`).
        resolved_id = item_id
        if contract.source == "github" and not item_id.startswith("#"):
            resolved_id = "#" + item_id
        try:
            with _open_warehouse() as con:
                data = render_lifecycle(
                    con, contract_id, contract.source, resolved_id
                )
        except ItemNotFound as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return templates.TemplateResponse(
            request,
            "item_lifecycle.html.jinja",
            {
                "title": f"{data.item_id} — {contract_id}",
                # The item id goes in the site header so the page
                # identifies itself at the top of the viewport.
                "metric_name": data.item_id,
                "contract": {"name": contract_id},
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
        contract: str,
        q: str | None = None,
        completed_on: str | None = None,
        sort: str = "completed_at",
        direction: str = "desc",
    ) -> HTMLResponse:
        """HTMX swap target for the work-items table: sort+filter
        round-trip to the server, return only the partial.

        `completed_on` is a YYYY-MM-DD date (UTC) that drills the
        table to a single day — used when the viewer clicks a bar
        on the throughput chart.
        """
        _ensure_contract_exists(contract)
        # Normalise sort/direction via the component's whitelist —
        # invalid values fall back to defaults inside render().
        sort_key: SortKey = sort if sort in (
            "item_id", "title", "created_at",
            "completed_at", "cycle_time_days"
        ) else "completed_at"
        direction_key: SortDir = direction if direction in (
            "asc", "desc"
        ) else "desc"
        with _open_warehouse() as con:
            data = render_work_items_table(
                con,
                contract,
                q=q,
                completed_on=completed_on,
                sort=sort_key,
                direction=direction_key,
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
                "contract": {"name": contract},
            },
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok"}

    return app
