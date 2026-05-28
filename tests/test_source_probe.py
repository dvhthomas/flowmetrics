"""Model-layer tests for `flowmetrics.source_probe`.

Source data-fetching for the builder lives in the model layer, not
the web layer — so it's unit-testable without standing up a FastAPI
app. These tests exercise it directly: mock `httpx.get`, assert the
parsing; the bucketing is pure so it needs no mocks.
"""

from __future__ import annotations

import httpx

from flowmetrics import source_probe as sp


class _Resp:
    def __init__(self, status_code=200, body=None, ctype="application/json"):
        self.status_code = status_code
        self._body = body if body is not None else []
        self.headers = {"content-type": ctype}

    def json(self):
        return self._body


class TestBucketItemsByStep:
    """Pure function — no I/O. The heart of the dry-run preview."""

    def _item(self, stage):
        return {"id": stage, "title": stage, "url": None, "current_stage": stage}

    def test_buckets_by_explicit_matches(self):
        steps = [
            {"name": "Ready", "wip": False, "matches": ["Open"]},
            {"name": "WIP", "wip": True, "matches": ["In Progress", "Review"]},
        ]
        items = [self._item("Open"), self._item("Review"), self._item("Review")]
        out = sp.bucket_items_by_step(items, steps)
        by = {b["step_name"]: b["count"] for b in out}
        assert by["Ready"] == 1
        assert by["WIP"] == 2
        assert by["_unmatched"] == 0

    def test_falls_back_to_name_when_no_matches(self):
        steps = [{"name": "Draft", "wip": False, "matches": []}]
        out = sp.bucket_items_by_step([self._item("Draft")], steps)
        assert out[0]["step_name"] == "Draft"
        assert out[0]["count"] == 1

    def test_unmatched_bucket_collects_strays(self):
        steps = [{"name": "WIP", "wip": True, "matches": ["In Progress"]}]
        out = sp.bucket_items_by_step(
            [self._item("In Progress"), self._item("Blocked")], steps
        )
        by = {b["step_name"]: b for b in out}
        assert by["_unmatched"]["count"] == 1
        assert by["_unmatched"]["items"][0]["current_stage"] == "Blocked"


class TestProbeSourceVocab:
    def test_github_parses_labels(self, monkeypatch):
        monkeypatch.setattr(httpx, "get",
            lambda *a, **k: _Resp(200, [{"name": "bug"}, {"name": "feat"}]))
        monkeypatch.setattr(
            "flowmetrics.sources.github.resolve_token", lambda: "t")
        out = sp.probe_source_vocab("github", {"repo": "o/r"})
        assert [x["name"] for x in out["labels"]] == ["bug", "feat"]
        assert len(out["lifecycle_events"]) == len(sp.GITHUB_LIFECYCLE_EVENTS)

    def test_jira_parses_distinct_statuses(self, monkeypatch):
        body = [
            {"statuses": [{"name": "Open"}, {"name": "In Progress"}]},
            {"statuses": [{"name": "In Progress"}, {"name": "Done"}]},
        ]
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, body))
        out = sp.probe_source_vocab(
            "jira", {"jira_url": "https://j", "jira_project": "X"})
        names = [s["name"] for s in out["labels"]]
        assert names == ["Open", "In Progress", "Done"]  # de-duped, ordered

    def test_github_api_failure_returns_empty_labels(self, monkeypatch):
        def boom(*a, **k):
            raise httpx.ConnectError("offline")
        monkeypatch.setattr(httpx, "get", boom)
        monkeypatch.setattr(
            "flowmetrics.sources.github.resolve_token", lambda: "t")
        out = sp.probe_source_vocab("github", {"repo": "o/r"})
        assert out["labels"] == []
        # Lifecycle constants still present so the UI isn't empty.
        assert out["lifecycle_events"]


class TestProbeSourceExists:
    def test_github_ok(self, monkeypatch):
        monkeypatch.setattr(httpx, "get",
            lambda *a, **k: _Resp(200, {"description": "Cool repo"}))
        monkeypatch.setattr(
            "flowmetrics.sources.github.resolve_token", lambda: "t")
        out = sp.probe_source_exists("github", {"repo": "o/r"})
        assert out == {"ok": True, "label": "Cool repo"}

    def test_github_404(self, monkeypatch):
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(404, {}))
        monkeypatch.setattr(
            "flowmetrics.sources.github.resolve_token", lambda: "t")
        out = sp.probe_source_exists("github", {"repo": "no/pe"})
        assert out["ok"] is False and "404" in out["error"]

    def test_github_bad_repo_format(self):
        out = sp.probe_source_exists("github", {"repo": "noslash"})
        assert out["ok"] is False


class TestDryRunFetch:
    def test_github_maps_pr_state(self, monkeypatch):
        body = {"items": [
            {"number": 1, "title": "open pr", "html_url": "u1", "state": "open"},
            {"number": 2, "title": "draft pr", "html_url": "u2",
             "state": "open", "draft": True},
            {"number": 3, "title": "merged", "html_url": "u3",
             "state": "closed", "pull_request": {"merged_at": "2026-01-01"}},
        ]}
        monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(200, body))
        monkeypatch.setattr(
            "flowmetrics.sources.github.resolve_token", lambda: "t")
        out = sp.dry_run_fetch(
            source="github", target={"repo": "o/r"},
            since="2026-04-01", items_cap=50)
        stages = {it["id"]: it["current_stage"] for it in out["items"]}
        assert stages["1"] == "PR opened"
        assert stages["2"] == "Draft"
        assert stages["3"] == "PR merged"
        assert out["window_to"] == "2026-05-01"

    def test_bad_since_returns_error(self):
        out = sp.dry_run_fetch(
            source="github", target={"repo": "o/r"},
            since="not-a-date", items_cap=10)
        assert out["stopped_by"] == "error"
