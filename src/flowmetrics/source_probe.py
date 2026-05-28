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
        when editing a materialised contract; empty here.

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

    items: list[dict] = []
    if source == "github":
        repo = (target or {}).get("repo") or ""
        if "/" not in repo:
            return {
                "items": [], "stopped_by": "error",
                "window_to": until_date.isoformat(),
            }
        url = (
            f"https://api.github.com/search/issues?q=repo:{repo}"
            f"+is:pr+updated:{since_date.isoformat()}.."
            f"{until_date.isoformat()}&per_page={min(items_cap, 100)}"
        )
        try:
            r = httpx.get(url, timeout=10.0, headers=github_headers())
            if r.status_code == 200:
                for it in r.json().get("items", [])[:items_cap]:
                    state = "PR closed"
                    if it.get("state") == "open":
                        state = "Draft" if it.get("draft") else "PR opened"
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

    stopped_by = "items_cap" if len(items) >= items_cap else "time_window"
    return {
        "items": items,
        "stopped_by": stopped_by,
        "window_to": until_date.isoformat(),
    }


def bucket_items_by_step(
    items: list[dict], steps: list[dict],
) -> list[dict]:
    """Bucket fetched items per the user's steps.

    Each item carries a `current_stage`. For each step, its
    effective matches = `step['matches']` or `[step['name']]`.
    Items matching no step land in a trailing `_unmatched` bucket.
    Pure — no I/O, fully unit-testable.
    """
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
        "step_name": "_unmatched", "wip": False, "matches": [],
        "count": len(unmatched), "items": unmatched,
    })
    return out
