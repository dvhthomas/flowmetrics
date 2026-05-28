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


# Curated lifecycle-event chips per source. The names are
# user-facing; the wip default per chip is the typical answer
# (clickable, overridable in the editor). These don't depend on
# the source API — they're constants that ship with the build.
_GITHUB_LIFECYCLE_EVENTS = (
    {"name": "PR opened", "wip": False},
    {"name": "Marked ready for review", "wip": True},
    {"name": "Changes requested", "wip": True},
    {"name": "Review approved", "wip": True},
    {"name": "PR merged", "wip": False},
    {"name": "PR closed without merge", "wip": False},
    {"name": "Issue opened", "wip": False},
    {"name": "Issue closed", "wip": False},
)
_JIRA_LIFECYCLE_EVENTS = (
    {"name": "Issue created", "wip": False},
    {"name": "Assigned", "wip": True},
    {"name": "Resolved", "wip": False},
    {"name": "Reopened", "wip": True},
    {"name": "Closed", "wip": False},
)


def _default_probe_source_vocab(kind: str, target: dict) -> dict:
    """Production source-vocab probe.

    Returns `{labels, lifecycle_events, warehouse_stages}`:
      - GitHub `labels`: `/repos/{owner}/{repo}/labels`.
      - Jira `labels`: `/rest/api/3/project/{key}/statuses`.
      - `lifecycle_events`: curated per source (constants above).
      - `warehouse_stages`: filled by the route from
        `contracts_db` + `warehouse/queries` if available.

    Tests stub via `app.state.probe_source_vocab`. Failures here
    return empty `labels` so the lifecycle + warehouse subsections
    still render."""
    import httpx

    labels: list[dict] = []
    lifecycle: list[dict] = []
    if kind == "github":
        lifecycle = list(_GITHUB_LIFECYCLE_EVENTS)
        repo = (target or {}).get("repo") or ""
        if "/" in repo:
            url = f"https://api.github.com/repos/{repo}/labels?per_page=100"
            try:
                r = httpx.get(url, timeout=10.0)
                if r.status_code == 200:
                    for label in r.json():
                        name = label.get("name")
                        if name:
                            labels.append({"name": name, "wip": True})
            except httpx.HTTPError:
                pass
    elif kind == "jira":
        lifecycle = list(_JIRA_LIFECYCLE_EVENTS)
        base = (target or {}).get("jira_url") or ""
        project = (target or {}).get("jira_project") or ""
        if base and project:
            # API v2 — Apache's public Jira (the documented demo) is
            # Jira Server, which exposes v2, not v3.
            url = f"{base.rstrip('/')}/rest/api/2/project/{project}/statuses"
            try:
                r = httpx.get(url, timeout=10.0)
                if r.status_code == 200:
                    seen: set[str] = set()
                    for issue_type in r.json():
                        for s in issue_type.get("statuses", []):
                            name = s.get("name")
                            if name and name not in seen:
                                seen.add(name)
                                labels.append({"name": name, "wip": True})
            except httpx.HTTPError:
                pass
    return {
        "labels": labels,
        "lifecycle_events": lifecycle,
        "warehouse_stages": [],
    }


def _bucket_items_by_step(
    items: list[dict], steps: list[dict],
) -> list[dict]:
    """Bucket fetched items per the user's steps.

    Each item carries a `current_stage` string. For each step,
    `effective_matches` = step['matches'] or (step['name'],) — the
    list of source-native identifiers this step captures.

    Items whose current stage doesn't match any step land in a
    special `_unmatched` bucket at the end of the list.
    """
    # Build a list of (step_dict, matches_tuple) preserving order.
    spec: list[dict] = []
    for s in steps:
        matches = s.get("matches") or []
        if not matches:
            matches = [s.get("name") or ""]
        spec.append({
            "step_name": s.get("name") or "",
            "wip": bool(s.get("wip")),
            "matches": list(matches),
            "items": [],
        })

    unmatched: list[dict] = []
    for item in items:
        stage = item.get("current_stage") or ""
        placed = False
        for bucket in spec:
            if stage in bucket["matches"]:
                bucket["items"].append(item)
                placed = True
                break
        if not placed:
            unmatched.append(item)

    out: list[dict] = []
    for bucket in spec:
        bucket["count"] = len(bucket["items"])
        out.append(bucket)
    out.append({
        "step_name": "_unmatched",
        "wip": False,
        "matches": [],
        "count": len(unmatched),
        "items": unmatched,
    })
    return out


def _default_dry_run_fetch(
    *, source: str, target: dict, since: str, items_cap: int,
) -> dict:
    """Production dry-run source fetch.

    Calls the existing source adapter to fetch items in the window
    `[since, since + 30 days]` capped at `items_cap`. Returns the
    list of items + which limit bit first ("items_cap" or
    "time_window").

    Items are dicts with `id, title, url, current_stage`. This
    layer keeps the dry-run independent of the warehouse
    materialise path — nothing gets written to disk.
    """
    from datetime import date, timedelta

    try:
        since_date = date.fromisoformat(since)
    except (TypeError, ValueError):
        return {
            "items": [], "stopped_by": "error",
            "window_to": None,
        }
    until_date = since_date + timedelta(days=30)

    items: list[dict] = []
    if source == "github":
        repo = (target or {}).get("repo") or ""
        if "/" not in repo:
            return {
                "items": [], "stopped_by": "error",
                "window_to": until_date.isoformat(),
            }
        # Minimal fetch via REST API (search PRs in the window).
        # Production-grade flow uses the existing source adapter;
        # this lightweight path keeps the dry-run independent of
        # the warehouse-writing materialise stack.
        import httpx

        url = (
            f"https://api.github.com/search/issues?q=repo:{repo}"
            f"+is:pr+updated:{since_date.isoformat()}.."
            f"{until_date.isoformat()}&per_page="
            f"{min(items_cap, 100)}"
        )
        try:
            r = httpx.get(url, timeout=10.0)
            if r.status_code == 200:
                for it in r.json().get("items", [])[:items_cap]:
                    state = "PR closed"
                    if it.get("state") == "open":
                        state = (
                            "Draft" if it.get("draft") else "PR opened"
                        )
                    elif it.get("pull_request", {}).get("merged_at"):
                        state = "PR merged"
                    items.append({
                        "id": str(it.get("number") or ""),
                        "title": it.get("title") or "",
                        "url": it.get("html_url"),
                        "current_stage": state,
                    })
        except httpx.HTTPError:
            pass
    elif source == "jira":
        base = (target or {}).get("jira_url") or ""
        project = (target or {}).get("jira_project") or ""
        if not (base and project):
            return {
                "items": [], "stopped_by": "error",
                "window_to": until_date.isoformat(),
            }
        import httpx

        jql = (
            f"project = {project} AND updated >= "
            f"\"{since_date.isoformat()}\" AND updated <= "
            f"\"{until_date.isoformat()}\""
        )
        url = (
            f"{base.rstrip('/')}/rest/api/2/search?jql="
            f"{httpx.QueryParams({'jql': jql})['jql']}"
            f"&maxResults={min(items_cap, 100)}"
            f"&fields=summary,status"
        )
        try:
            r = httpx.get(url, timeout=10.0)
            if r.status_code == 200:
                for it in r.json().get("issues", [])[:items_cap]:
                    fields = it.get("fields") or {}
                    status = (fields.get("status") or {}).get("name") or ""
                    items.append({
                        "id": it.get("key") or "",
                        "title": fields.get("summary") or "",
                        "url": f"{base.rstrip('/')}/browse/{it.get('key')}",
                        "current_stage": status,
                    })
        except httpx.HTTPError:
            pass

    stopped_by = (
        "items_cap" if len(items) >= items_cap else "time_window"
    )
    return {
        "items": items,
        "stopped_by": stopped_by,
        "window_to": until_date.isoformat(),
    }


def _default_probe_stages(kind: str, target: dict) -> dict:
    """Production stage-discovery probe.

    Strategy: read the existing warehouse's `transitions` table for
    this target's contract — that table already has the canonical
    stage names. If no warehouse exists yet, return an empty list +
    hint pointing the user at `flow materialise`.

    This avoids running a full materialise from inside a web
    request (which would be slow + race-prone). The user materialises
    once to seed, comes back to the wizard, and gets discovered
    stages instantly.

    The wizard's caller passes the workflow id alongside (via the
    `name` field in the request body — see probe_stages). If not
    set, fall back to the source-target hash."""
    # The default implementation needs more wiring than a single
    # module-level helper to find the right data_dir — it's called
    # from inside the route which has the dir in scope. The route
    # injects a closure-bound implementation in production via
    # `app.state.probe_stages = ...` during create_app. The bare
    # module-level fallback below is for tests + the no-warehouse
    # case.
    return {
        "stages": [],
        "hint": (
            "no stages found in the warehouse. Run "
            "`flow materialise <name>` against this source first, "
            "then re-probe."
        ),
    }


def _default_probe_source(kind: str, target: dict) -> dict:
    """Production source-existence probe. GitHub: HEAD the repo
    page; Jira: GET the project endpoint. Returns the
    {ok, label?, error?} contract the wizard expects.

    Kept module-level (not closed-over) so the test suite can stub
    it via `app.state.probe_source` without owning a network mock."""
    import httpx

    if kind == "github":
        repo = (target or {}).get("repo") or ""
        if "/" not in repo:
            return {"ok": False, "error": "repo must be OWNER/NAME"}
        url = f"https://api.github.com/repos/{repo}"
        try:
            r = httpx.get(url, timeout=10.0)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}
        if r.status_code == 404:
            return {"ok": False, "error": "repo not found (404)"}
        if r.status_code >= 400:
            return {
                "ok": False,
                "error": f"{url} returned {r.status_code}",
            }
        return {
            "ok": True,
            "label": r.json().get("description") or repo,
        }
    if kind == "jira":
        base = (target or {}).get("jira_url") or ""
        project = (target or {}).get("jira_project") or ""
        if not (base and project):
            return {
                "ok": False,
                "error": "jira needs both jira_url and jira_project",
            }
        # API v2 — Apache's public Jira is Jira Server.
        url = f"{base.rstrip('/')}/rest/api/2/project/{project}"
        try:
            r = httpx.get(url, timeout=10.0)
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}
        if r.status_code == 404:
            return {
                "ok": False,
                "error": f"project {project!r} not found",
            }
        if r.status_code >= 400:
            return {
                "ok": False,
                "error": f"{url} returned {r.status_code}",
            }
        body = r.json() if r.headers.get("content-type", "").startswith(
            "application/json"
        ) else {}
        return {"ok": True, "label": body.get("name", project)}
    return {"ok": False, "error": f"unknown source {kind!r}"}


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
        contracts_db=None,
    ) -> None:
        self.id = workflow_id
        self._data_dir = data_dir
        # Load the contract via the DB when one is supplied (the
        # runtime path); fall back to the legacy filesystem path
        # for any code path that hasn't been wired through yet.
        if contracts_db is not None:
            meta = contracts_db.get_meta(workflow_id)
            if meta is None or meta.archived_at is not None:
                from .contract import ContractError
                raise ContractError(
                    f"contract {workflow_id!r} not in the live "
                    f"store at {contracts_dir / 'contracts.db'}"
                )
            self.contract = meta.contract
        else:
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
    # First-boot migration: import any legacy YAMLs in the workflows
    # dir into the SQLite store, then move them to `migrated/`.
    # Idempotent — subsequent calls with no YAMLs are no-ops.
    from .contracts_db import ContractsDB, ensure_initialized
    ensure_initialized(contracts_dir)
    contracts_db = ContractsDB(contracts_dir / "contracts.db")

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
            contracts_db=contracts_db,
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
        """Confirm a contract exists in the live store; otherwise
        404 with a clear message."""
        if contracts_db.get_meta(name) is None or _is_archived(name):
            raise HTTPException(
                status_code=404,
                detail=(
                    f"contract {name!r} not found in the workflows "
                    f"store at {contracts_dir / 'contracts.db'}. "
                    "Create one through /admin/contracts/new, or "
                    "drop a YAML in this directory and restart."
                ),
            )

    def _is_archived(name: str) -> bool:
        meta = contracts_db.get_meta(name)
        return meta is not None and meta.archived_at is not None

    def _first_contract_or_404() -> str:
        """Pick the first live contract, or 404 with a clear
        message if there are none."""
        rows = contracts_db.list()
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"no live contracts in the store at "
                    f"{contracts_dir / 'contracts.db'}. Create one "
                    "through /admin/contracts/new, or drop a YAML "
                    "in this directory and restart."
                ),
            )
        return rows[0].name

    def _available_contracts() -> list[str]:
        """Live contract ids, alphabetical."""
        return [m.name for m in contracts_db.list()]

    def _raw_yaml_text(name: str) -> str | None:
        """The contract's canonical YAML body — returned to the UI
        for the textarea-fallback / diff view. None when the row
        is missing."""
        meta = contracts_db.get_meta(name)
        return meta.yaml if meta else None

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
        """List-row payload. Reads from the DB."""
        c = contracts_db.get(name)
        if c is None:
            return {"id": name, "label": name, "source": None}
        return {
            "id": c.name,
            "label": c.label or c.name,
            "source": c.source,
        }

    def _load_contract_for_request(name: str):
        """Load a Contract from the DB by id. Raises HTTPException on
        404 (missing) or 500 (parser failure, which shouldn't happen
        since the DB only stores YAMLs that passed validation on
        write, but be defensive)."""
        meta = contracts_db.get_meta(name)
        if meta is None or meta.archived_at is not None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"contract {name!r} not found in "
                    f"{contracts_dir / 'contracts.db'}."
                ),
            )
        return meta.contract

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
            c = contracts_db.get(name)
            label = c.label if c and c.label else name
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
    def list_contracts(include_archived: bool = False) -> list[dict]:
        """List contracts in the live store.

        Default: archived rows are excluded. Pass
        `?include_archived=true` to surface them — each entry then
        carries an `archived: bool` flag.
        """
        out = []
        for meta in contracts_db.list(include_archived=include_archived):
            entry = {
                "id": meta.contract.name,
                "label": meta.contract.label or meta.contract.name,
                "source": meta.contract.source,
            }
            if include_archived:
                entry["archived"] = meta.archived_at is not None
                entry["archived_at"] = meta.archived_at
                entry["archived_reason"] = meta.archived_reason
            out.append(entry)
        return out

    @app.get("/api/internal/contracts/{contract_id}", dependencies=auth_dep)
    def get_contract(
        contract_id: str, include_archived: bool = False,
    ) -> dict:
        """Full detail for one contract.

        Default: archived rows return 404. Pass
        `?include_archived=true` to fetch an archived row; the
        response carries `archived: true` + `archived_at` +
        `archived_reason`."""
        meta = contracts_db.get_meta(contract_id)
        if meta is None:
            raise HTTPException(
                status_code=404,
                detail=f"contract {contract_id!r} not found",
            )
        if meta.archived_at is not None and not include_archived:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"contract {contract_id!r} is archived. "
                    "Pass ?include_archived=true to fetch."
                ),
            )
        c = meta.contract
        parsed: dict = {
            "name": c.name,
            "source": c.source,
            "repo": c.repo,
            "jira_url": c.jira_url,
            "jira_project": c.jira_project,
            "start": c.start.isoformat() if c.start else None,
            "stop": c.stop.isoformat() if c.stop else None,
            "label": c.label,
            # Canonical: ordered list of steps with matches.
            "steps": [
                {"name": s.name, "wip": s.wip, "matches": list(s.matches)}
                for s in c.steps
            ],
        }
        # Legacy shim for any consumer still reading the 3-bucket
        # shape (older test fixtures, the CFD/Aging code paths).
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
            "yaml": meta.yaml,
            "materialise": _materialise_status(contract_id),
            "archived": meta.archived_at is not None,
            "archived_at": meta.archived_at,
            "archived_reason": meta.archived_reason,
        }

    @app.get(
        "/admin/contracts/new",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def new_contract_wizard(request: Request) -> HTMLResponse:
        """The new-contract wizard. Pure form + JS — the actual
        write goes through PUT /api/internal/contracts/{id}."""
        return templates.TemplateResponse(
            request,
            "contracts_new.html.jinja",
            {
                "title": "New workflow · flowmetrics",
                "contract": None,
                "contracts_dir_display": str(contracts_dir.resolve()),
                "wizard_mode": "new",
                "edit_id": None,
            },
        )

    @app.get(
        "/admin/contracts/{contract_id}/edit",
        response_class=HTMLResponse,
        dependencies=auth_dep,
    )
    def edit_contract_page(
        request: Request, contract_id: str,
    ) -> HTMLResponse:
        """Edit-existing-contract page. Reuses the wizard template;
        the JS detects `mode=edit` and hydrates fields from
        GET /api/internal/contracts/{id} on load."""
        _ensure_contract_exists(contract_id)
        return templates.TemplateResponse(
            request,
            "contracts_new.html.jinja",
            {
                "title": f"Edit {contract_id} · flowmetrics",
                "contract": None,
                "contracts_dir_display": str(contracts_dir.resolve()),
                "wizard_mode": "edit",
                "edit_id": contract_id,
            },
        )

    @app.post(
        "/api/internal/contracts/_dry-run",
        dependencies=write_dep,
    )
    def dry_run(payload: dict, request: Request) -> dict:
        """Preview a contract definition against live source data.

        Takes the in-progress contract payload + `{since, items_cap}`,
        calls a bounded fetch (≤200 items OR ≤30 days from `since`),
        and buckets items per the user's steps using
        `Step.effective_matches`. Items whose current stage doesn't
        map to any step land in `_unmatched`.

        Cached 5 minutes per (source target, since, items_cap, steps
        signature). `?force=true` busts.

        Items NEVER enter the warehouse — the dry-run is ephemeral.
        """
        c_payload = payload.get("contract") or {}
        source = c_payload.get("source")
        if source not in ("github", "jira"):
            raise HTTPException(
                status_code=422,
                detail=f"unknown source {source!r}; pick github or jira.",
            )
        since = payload.get("since") or ""
        items_cap = int(payload.get("items_cap") or 200)
        steps = c_payload.get("steps") or []

        # Cache key: target tuple + cap params + a stable signature
        # of the user's steps. Changing the steps re-buckets locally
        # but the underlying fetch is the same — could optimise to
        # reuse the fetch across step changes; for MVP we key on
        # steps too to keep behaviour predictable.
        target = {
            "repo": c_payload.get("repo"),
            "jira_url": c_payload.get("jira_url"),
            "jira_project": c_payload.get("jira_project"),
        }
        import hashlib
        import json as _json
        steps_sig = hashlib.sha256(
            _json.dumps(steps, sort_keys=True).encode()
        ).hexdigest()[:16]
        cache_key = (
            "dry-run", source, target.get("repo"), target.get("jira_url"),
            target.get("jira_project"), since, items_cap, steps_sig,
        )
        force = (request.query_params.get("force") or "").lower() in (
            "true", "1", "yes",
        )
        cache: dict = getattr(app.state, "_dry_run_cache", {})
        if not hasattr(app.state, "_dry_run_cache"):
            app.state._dry_run_cache = cache
        now = datetime.now(UTC).timestamp()
        if not force:
            cached = cache.get(cache_key)
            if cached is not None and now - cached[0] < 5 * 60:
                return cached[1]

        # Fetch (or stub via the injection slot for tests).
        fetcher = getattr(
            app.state, "dry_run_fetch", _default_dry_run_fetch,
        )
        try:
            fetched = fetcher(
                source=source, target=target,
                since=since, items_cap=items_cap,
            )
        except Exception as exc:
            return {
                "fetched_at": datetime.now(UTC).isoformat(),
                "expires_at": datetime.now(UTC).isoformat(),
                "stopped_by": "error",
                "items_fetched": 0,
                "window": {"from": since, "to": None},
                "per_step": [],
                "error": f"fetch failed: {exc}",
            }

        items = fetched.get("items", [])
        per_step = _bucket_items_by_step(items, steps)

        fetched_at = datetime.now(UTC)
        expires_at = datetime.fromtimestamp(now + 5 * 60, tz=UTC)
        result = {
            "fetched_at": fetched_at.isoformat(),
            "expires_at": expires_at.isoformat(),
            "stopped_by": fetched.get("stopped_by", "items_cap"),
            "items_fetched": len(items),
            "window": {"from": since, "to": fetched.get("window_to")},
            "per_step": per_step,
        }
        cache[cache_key] = (now, result)
        return result

    @app.post(
        "/api/internal/contracts/_probe-source-vocab",
        dependencies=write_dep,
    )
    def probe_source_vocab(payload: dict, request: Request) -> dict:
        """Source-vocab probe — richer successor to `_probe-stages`.
        Returns `{labels, lifecycle_events, warehouse_stages}` for
        the Steps editor's Suggestions panel."""
        source = payload.get("source")
        if source not in ("github", "jira"):
            raise HTTPException(
                status_code=422,
                detail=f"unknown source {source!r}; pick github or jira.",
            )
        target = {
            "repo": payload.get("repo"),
            "jira_url": payload.get("jira_url"),
            "jira_project": payload.get("jira_project"),
        }
        key = ("vocab", source, target.get("repo"),
               target.get("jira_url"), target.get("jira_project"))
        force = (request.query_params.get("force") or "").lower() in (
            "true", "1", "yes",
        )
        cache: dict = getattr(app.state, "_probe_vocab_cache", {})
        if not hasattr(app.state, "_probe_vocab_cache"):
            app.state._probe_vocab_cache = cache
        now = datetime.now(UTC).timestamp()
        if not force:
            cached = cache.get(key)
            if cached is not None and now - cached[0] < 15 * 60:
                return cached[1]

        probe = getattr(
            app.state, "probe_source_vocab", _default_probe_source_vocab,
        )
        try:
            result = probe(source, target)
        except Exception as exc:
            return {
                "labels": [],
                "lifecycle_events": (
                    list(_GITHUB_LIFECYCLE_EVENTS) if source == "github"
                    else list(_JIRA_LIFECYCLE_EVENTS)
                ),
                "warehouse_stages": [],
                "hint": f"probe failed: {exc}",
            }
        cache[key] = (now, result)
        return result

    @app.post(
        "/api/internal/contracts/_probe-stages", dependencies=write_dep,
    )
    def probe_stages(payload: dict, request: Request) -> dict:
        """Discover the workflow stages from the source. Runs a
        bounded materialise into a scratch dir (last 30 days, no
        status file), reads the transitions to extract distinct
        stage names, deletes the scratch dir, returns `{stages,
        hint?}`. Caches per-target for 15 minutes so the wizard's
        iteration loop doesn't re-pay the API.

        The probe callable is injected via `app.state.probe_stages`
        — tests stub it. Production wiring uses
        `_default_probe_stages` (below)."""
        source = payload.get("source")
        if source not in ("github", "jira"):
            raise HTTPException(
                status_code=422,
                detail=f"unknown source {source!r}; pick github or jira.",
            )
        target = {
            "repo": payload.get("repo"),
            "jira_url": payload.get("jira_url"),
            "jira_project": payload.get("jira_project"),
        }
        # Cache key: deterministic tuple of just the identifying
        # bits so a probe of repo "a/b" hits regardless of incidental
        # payload fields.
        key = (source, target.get("repo"),
               target.get("jira_url"), target.get("jira_project"))
        force = (request.query_params.get("force") or "").lower() in (
            "true", "1", "yes",
        )
        cache: dict = getattr(app.state, "_probe_stages_cache", {})
        if not hasattr(app.state, "_probe_stages_cache"):
            app.state._probe_stages_cache = cache
        now = datetime.now(UTC).timestamp()
        if not force:
            cached = cache.get(key)
            if cached is not None and now - cached[0] < 15 * 60:
                return cached[1]

        probe = getattr(app.state, "probe_stages", _default_probe_stages)
        try:
            result = probe(source, target)
        except Exception as exc:
            return {"stages": [], "hint": f"probe failed: {exc}"}
        cache[key] = (now, result)
        return result

    @app.post(
        "/api/internal/contracts/_probe-source", dependencies=write_dep,
    )
    def probe_source(payload: dict) -> dict:
        """Check whether the named source target (GitHub repo or
        Jira project) actually exists. The probe callable is
        injected via `app.state.probe_source` so tests can avoid
        real network calls; production wiring uses a small httpx
        helper (see `_default_probe_source`)."""
        source = payload.get("source")
        if source not in ("github", "jira"):
            raise HTTPException(
                status_code=422,
                detail=f"unknown source {source!r}; pick github or jira.",
            )
        target = {
            "repo": payload.get("repo"),
            "jira_url": payload.get("jira_url"),
            "jira_project": payload.get("jira_project"),
        }
        probe = getattr(app.state, "probe_source", _default_probe_source)
        try:
            return probe(source, target)
        except Exception as exc:
            return {"ok": False, "error": f"probe failed: {exc}"}

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
        """Create or overwrite a contract. Body must carry a `yaml`
        STRING that parses against `parse_contract_text` with
        `name == contract_id`. The DB owns the storage — no
        filesystem write."""
        from .contracts_db import ContractsDBError

        text = payload.get("yaml") or ""
        try:
            contract = parse_contract_text(text, contract_id)
        except ContractError as exc:
            errors = validate_yaml_text_structured(text, contract_id)
            raise HTTPException(
                status_code=422,
                detail={"message": str(exc), "errors": errors},
            ) from exc
        try:
            contracts_db.put(contract)
        except ContractsDBError as exc:
            # e.g. id collides with an archived row.
            raise HTTPException(
                status_code=409, detail=str(exc),
            ) from exc
        return get_contract(contract_id)

    @app.post(
        "/api/internal/contracts/{contract_id}/archive",
        dependencies=write_dep,
    )
    def archive_contract(contract_id: str, payload: dict | None = None) -> dict:
        """Soft-delete: sets `archived_at` (and an optional reason).
        Idempotent — re-archiving a row keeps the original timestamp;
        only the reason rolls forward."""
        from .contracts_db import ContractsDBError

        meta = contracts_db.get_meta(contract_id)
        if meta is None:
            raise HTTPException(
                status_code=404,
                detail=f"contract {contract_id!r} not found",
            )
        reason = (payload or {}).get("reason")
        try:
            contracts_db.archive(contract_id, reason=reason)
        except ContractsDBError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": contract_id, "archived": True, "reason": reason}

    @app.post(
        "/api/internal/contracts/{contract_id}/restore",
        dependencies=write_dep,
    )
    def restore_contract(contract_id: str) -> dict:
        """Undo archive. No-op when the row is already live."""
        from .contracts_db import ContractsDBError

        meta = contracts_db.get_meta(contract_id)
        if meta is None:
            raise HTTPException(
                status_code=404,
                detail=f"contract {contract_id!r} not found",
            )
        try:
            contracts_db.restore(contract_id)
        except ContractsDBError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": contract_id, "archived": False}

    @app.delete(
        "/api/internal/contracts/{contract_id}", dependencies=write_dep,
    )
    async def delete_contract(
        contract_id: str, request: Request,
    ) -> dict:
        """**Hard delete.** Refuses unless the contract is already
        archived (the two-step delete invariant — POST to /archive
        first). Independent of `purge_data`, which controls whether
        the warehouse partitions get wiped alongside the row."""
        from .contracts_db import ContractsDBError

        meta = contracts_db.get_meta(contract_id)
        if meta is None:
            raise HTTPException(
                status_code=404,
                detail=f"contract {contract_id!r} not found",
            )
        if meta.archived_at is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"contract {contract_id!r} is live; archive it "
                    "first (POST /api/internal/contracts/"
                    f"{contract_id}/archive) before hard-deleting."
                ),
            )

        # Read purge_data from either the query string OR a JSON body.
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

        try:
            contracts_db.hard_delete(contract_id)
        except ContractsDBError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        if purge:
            import shutil

            partition_dirs = [
                data_dir / "work_items" / f"contract_id={contract_id}",
                data_dir / "transitions" / f"contract_id={contract_id}",
                data_dir / "runs" / contract_id,
            ]
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
