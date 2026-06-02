"""Source-probe model layer for the contract builder.

Everything that talks to GitHub / Jira on behalf of the builder UI
lives here — NOT in the web layer (app.py). Keeping it in a model
module means it's unit-testable in isolation (mock `httpx.get`,
assert the parsing) without standing up a FastAPI app.

Three capabilities, all pure functions over (source, target):

  - `probe_source_exists`  — does this repo / project exist?
    Returns {ok, label?, error?}.
  - `probe_source_vocab`   — the source's vocabulary for the Steps
    editor: {labels, lifecycle_events, warehouse_stages}.
  - `dry_run_fetch`        — a bounded fetch of real items
    (≤ items_cap or 30 days) with their current stage, for the
    dry-run preview. Plus `bucket_items_by_step`, the pure
    bucketing helper.

GitHub calls reuse the app's `resolve_token` so they get the
authenticated 5000/hour limit (and private-repo access) instead of
the 60/hour anonymous limit. Jira uses API v2 (Apache's public
instance — the documented demo — is Jira Server).
"""

from __future__ import annotations

from datetime import date, timedelta

import httpx

from . import signals

# Curated lifecycle-event chips per source. User-facing names; the
# `wip` per chip is the typical default (clickable, overridable in
# the editor). Constants — no source API call needed.
GITHUB_LIFECYCLE_EVENTS = (
    {"name": "PR opened", "wip": False},
    {"name": "Marked ready for review", "wip": True},
    {"name": "Changes requested", "wip": True},
    {"name": "Review approved", "wip": True},
    {"name": "PR merged", "wip": False},
    {"name": "PR closed without merge", "wip": False},
    {"name": "Issue opened", "wip": False},
    {"name": "Issue closed", "wip": False},
)
JIRA_LIFECYCLE_EVENTS = (
    {"name": "Issue created", "wip": False},
    {"name": "Assigned", "wip": True},
    {"name": "Resolved", "wip": False},
    {"name": "Reopened", "wip": True},
    {"name": "Closed", "wip": False},
)


def github_headers() -> dict[str, str]:
    """Auth headers for GitHub REST calls. Reuses the app's
    `resolve_token` ($GITHUB_TOKEN or `gh auth token`) so calls get
    the 5000/hour authenticated limit + private-repo access.
    Degrades to anonymous when no token is configured."""
    headers = {"Accept": "application/vnd.github+json"}
    try:
        from .sources.github import resolve_token
        token = resolve_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
    except Exception:
        pass  # anonymous — rate-limited but functional
    return headers


# ---------------------------------------------------------------------------
# Existence probe.
# ---------------------------------------------------------------------------


def probe_source_exists(source: str, target: dict) -> dict:
    """Does the named repo / project exist? Returns
    {ok, label?, error?}."""
    if source == "github":
        repo = (target or {}).get("repo") or ""
        if "/" not in repo:
            return {"ok": False, "error": "repo must be OWNER/NAME"}
        url = f"https://api.github.com/repos/{repo}"
        try:
            r = httpx.get(url, timeout=10.0, headers=github_headers())
        except httpx.HTTPError as exc:
            return {"ok": False, "error": str(exc)}
        if r.status_code == 404:
            return {"ok": False, "error": "repo not found (404)"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"{url} returned {r.status_code}"}
        return {"ok": True, "label": r.json().get("description") or repo}
    if source == "jira":
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
            return {"ok": False, "error": f"project {project!r} not found"}
        if r.status_code >= 400:
            return {"ok": False, "error": f"{url} returned {r.status_code}"}
        body = r.json() if r.headers.get("content-type", "").startswith(
            "application/json"
        ) else {}
        return {"ok": True, "label": body.get("name", project)}
    return {"ok": False, "error": f"unknown source {source!r}"}


# ---------------------------------------------------------------------------
# Vocabulary probe (labels / statuses + lifecycle + warehouse stages).
# ---------------------------------------------------------------------------


def probe_source_vocab(source: str, target: dict) -> dict:
    """The source's vocabulary for the Steps editor.

    Returns `{labels, lifecycle_events, warehouse_stages}`:
      - GitHub `labels`: `/repos/{owner}/{repo}/labels`.
      - Jira `labels`: `/rest/api/2/project/{key}/statuses`.
      - `lifecycle_events`: curated per source (constants above).
      - `warehouse_stages`: the route fills this from the warehouse
        when editing a materialized contract; empty here.

    Source-API failures return empty `labels` so the lifecycle +
    warehouse subsections still render.
    """
    labels: list[dict] = []
    lifecycle: list[dict] = []
    if source == "github":
        lifecycle = list(GITHUB_LIFECYCLE_EVENTS)
        repo = (target or {}).get("repo") or ""
        if "/" in repo:
            url = f"https://api.github.com/repos/{repo}/labels?per_page=100"
            try:
                r = httpx.get(url, timeout=10.0, headers=github_headers())
                if r.status_code == 200:
                    for label in r.json():
                        name = label.get("name")
                        if name:
                            labels.append({"name": name, "wip": True})
            except httpx.HTTPError:
                pass
    elif source == "jira":
        lifecycle = list(JIRA_LIFECYCLE_EVENTS)
        base = (target or {}).get("jira_url") or ""
        project = (target or {}).get("jira_project") or ""
        if base and project:
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


# ---------------------------------------------------------------------------
# Dry-run: bounded fetch + per-step bucketing.
# ---------------------------------------------------------------------------


def _github_pr_stage(it: dict) -> str:
    """Map a GitHub search-result PR to the dry-run preview stage."""
    if it.get("state") == "open":
        return "Draft" if it.get("draft") else "PR opened"
    if it.get("pull_request", {}).get("merged_at"):
        return "PR merged"
    return "PR closed"


def _github_pr_signal(it: dict) -> str | None:
    """Representative lifecycle signal for a PR's *current* state, so the
    preview's event matchers line up with the shared evaluator. The
    snapshot can't replay history, so this reflects the latest state."""
    if it.get("state") == "open":
        return signals.SIGNAL_GITHUB_PR_CREATED
    if it.get("pull_request", {}).get("merged_at"):
        return signals.SIGNAL_GITHUB_PR_MERGED
    return None  # closed-without-merge has no modelled signal


def _github_dry_run_items(
    repo: str, since_date: date, until_date: date, items_cap: int,
) -> list[dict]:
    url = (
        f"https://api.github.com/search/issues?q=repo:{repo}"
        f"+is:pr+updated:{since_date.isoformat()}.."
        f"{until_date.isoformat()}&per_page={min(items_cap, 100)}"
    )
    try:
        r = httpx.get(url, timeout=10.0, headers=github_headers())
    except httpx.HTTPError:
        return []
    if r.status_code != 200:
        return []
    return [
        {
            "id": str(it.get("number") or ""),
            "title": it.get("title") or "",
            "url": it.get("html_url"),
            "current_stage": _github_pr_stage(it),
            "signal": _github_pr_signal(it),
        }
        for it in r.json().get("items", [])[:items_cap]
    ]


def _jira_dry_run_items(
    base: str, project: str, since_date: date, until_date: date, items_cap: int,
) -> list[dict]:
    jql = (
        f"project = {project} AND updated >= "
        f"\"{since_date.isoformat()}\" AND updated <= "
        f"\"{until_date.isoformat()}\""
    )
    url = (
        f"{base.rstrip('/')}/rest/api/2/search?jql="
        f"{httpx.QueryParams({'jql': jql})['jql']}"
        f"&maxResults={min(items_cap, 100)}&fields=summary,status"
    )
    try:
        r = httpx.get(url, timeout=10.0)
    except httpx.HTTPError:
        return []
    if r.status_code != 200:
        return []
    out: list[dict] = []
    for it in r.json().get("issues", [])[:items_cap]:
        fields = it.get("fields") or {}
        out.append({
            "id": it.get("key") or "",
            "title": fields.get("summary") or "",
            "url": f"{base.rstrip('/')}/browse/{it.get('key')}",
            "current_stage": (fields.get("status") or {}).get("name") or "",
            # The snapshot shows current status; represent it as a
            # status-change so `event: status-changed` matchers preview.
            "signal": signals.SIGNAL_JIRA_STATUS_CHANGED,
        })
    return out


def dry_run_fetch(
    *, source: str, target: dict, since: str, items_cap: int,
) -> dict:
    """Bounded fetch of real items for the dry-run preview.

    Window is `[since, since + 30 days]`, capped at `items_cap`.
    Returns the items + which limit bit first ("items_cap" or
    "time_window"). Items are dicts with
    `id, title, url, current_stage`. Nothing is written to the
    warehouse — the dry-run is ephemeral.
    """
    try:
        since_date = date.fromisoformat(since)
    except (TypeError, ValueError):
        return {"items": [], "stopped_by": "error", "window_to": None}
    until_date = since_date + timedelta(days=30)
    window_to = until_date.isoformat()
    target = target or {}
    err = {"items": [], "stopped_by": "error", "window_to": window_to}

    if source == "github":
        repo = target.get("repo") or ""
        if "/" not in repo:
            return err
        items = _github_dry_run_items(repo, since_date, until_date, items_cap)
    elif source == "jira":
        base = target.get("jira_url") or ""
        project = target.get("jira_project") or ""
        if not (base and project):
            return err
        items = _jira_dry_run_items(base, project, since_date, until_date, items_cap)
    else:
        items = []

    stopped_by = "items_cap" if len(items) >= items_cap else "time_window"
    return {"items": items, "stopped_by": stopped_by, "window_to": window_to}


def bucket_items_by_step(
    items: list[dict], steps: list[dict], *, source: str = "github",
) -> list[dict]:
    """Bucket fetched items per the user's steps.

    Each item carries `current_stage` (and, for event matchers, a
    representative `signal`). Steps carry typed matchers; bucketing
    uses the SAME `matching` evaluator as the materialize remap, so the
    preview can't drift from what materialize will write. Items matching
    no step land in a trailing `_unmatched` bucket. Pure — no I/O.
    """
    from .workflow import Step
    from .matching import step_for

    parsed: list[Step] = []
    for s in steps:
        try:
            parsed.append(Step(
                name=s.get("name") or "",
                wip=bool(s.get("wip")),
                matches=s.get("matches") or [],
            ))
        except Exception:
            # A malformed step shouldn't crash the preview; skip it.
            continue

    spec = [
        {"step_name": st.name, "wip": st.wip, "items": []}
        for st in parsed
    ]
    unmatched: list[dict] = []
    for item in items:
        stage = item.get("current_stage") or ""
        signal = item.get("signal")
        placed = False
        for st, bucket in zip(parsed, spec, strict=True):
            if step_for(st, source=source, stage=stage, signal=signal):
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
        "step_name": "_unmatched", "wip": False,
        "count": len(unmatched), "items": unmatched,
    })
    return out
