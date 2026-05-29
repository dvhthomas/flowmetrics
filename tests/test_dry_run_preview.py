"""C5 — Dry-run preview against the live source.

`POST /api/internal/contracts/_dry-run` takes the in-progress
contract payload + `{since, items_cap}` and returns items bucketed
per the user's steps — without persisting anything to the
warehouse.

Cap: stop at the smaller of `items_cap` (default 200) or a 30-day
window from `since`. The cap that bit first is reported back.

Per-step bucketing uses `Step.effective_matches` (the C4 matches
list, or the step's name when matches is empty). Items whose
current stage doesn't map to any step land in an `_unmatched`
bucket — the "your workflow is missing a step" signal.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from flowmetrics.app import create_app


@pytest.fixture
def workspace(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    data = tmp_path / "data"
    return contracts, data


def _payload(steps, source="github", repo="owner/repo"):
    contract = {"name": "tmp", "source": source}
    if source == "github":
        contract["repo"] = repo
    else:
        contract["jira_url"] = "https://j.example.com"
        contract["jira_project"] = "X"
    contract["steps"] = steps
    return {"contract": contract, "since": "2026-04-01", "items_cap": 200}


def _post(client, payload, mock_fetch=None):
    if mock_fetch is not None:
        client.app.state.dry_run_fetch = mock_fetch
    return client.post(
        "/api/internal/contracts/_dry-run",
        json=payload, headers={"X-Requested-With": "fetch"},
    )


def _item(stage: str, item_id: str | None = None) -> dict:
    """Bare-bones item shape consumed by the bucketing logic.
    The dry-run endpoint expects each fetched item to carry
    `current_stage`, `id`, `title`, and `url`."""
    return {
        "id": item_id or f"#{stage[0]}{len(stage)}",
        "title": f"Item for {stage}",
        "url": None,
        "current_stage": stage,
        "fetched_at": datetime(2026, 4, 15, tzinfo=UTC).isoformat(),
    }


class TestEndpointShape:
    def test_returns_per_step_buckets(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        steps = [
            {"name": "Ready", "wip": False, "matches": [{"stage": "Open"}]},
            {"name": "WIP", "wip": True, "matches": [{"stage": "In Progress"}, {"stage": "Review"}]},
            {"name": "Done", "wip": False, "matches": [{"stage": "Closed"}]},
        ]
        items = [
            _item("Open"), _item("Open"),
            _item("In Progress"),
            _item("Review"), _item("Review"),
            _item("Closed"),
        ]
        with TestClient(app) as client:
            r = _post(
                client, _payload(steps),
                mock_fetch=lambda **kw: {
                    "items": items, "stopped_by": "items_cap",
                    "window_to": "2026-04-30",
                },
            )
        assert r.status_code == 200, r.text
        body = r.json()
        by_name = {p["step_name"]: p for p in body["per_step"]}
        assert by_name["Ready"]["count"] == 2
        assert by_name["WIP"]["count"] == 3
        assert by_name["Done"]["count"] == 1

    def test_carries_response_metadata(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = _post(
                client,
                _payload([{"name": "WIP", "wip": True, "matches": [{"stage": "X"}]}]),
                mock_fetch=lambda **kw: {
                    "items": [_item("X")],
                    "stopped_by": "items_cap",
                    "window_to": "2026-04-30",
                },
            )
        body = r.json()
        assert body["items_fetched"] == 1
        assert body["stopped_by"] in ("items_cap", "time_window")
        assert body["window"]["from"] == "2026-04-01"
        assert body["window"]["to"] == "2026-04-30"
        assert "fetched_at" in body
        assert "expires_at" in body


class TestUnmatchedBucket:
    """Items whose current stage doesn't match any step's
    effective_matches land in a special `_unmatched` bucket. This
    is the "your workflow is missing a step" signal."""

    def test_unmatched_items_surface_as_their_own_bucket(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        steps = [
            {"name": "WIP", "wip": True, "matches": [{"stage": "In Progress"}]},
        ]
        items = [
            _item("In Progress"),
            _item("Blocked"),       # not in any step's matches
            _item("Awaiting QA"),   # not in any step's matches
        ]
        with TestClient(app) as client:
            r = _post(
                client, _payload(steps),
                mock_fetch=lambda **kw: {
                    "items": items, "stopped_by": "items_cap",
                    "window_to": "2026-04-30",
                },
            )
        body = r.json()
        by_name = {p["step_name"]: p for p in body["per_step"]}
        assert by_name["WIP"]["count"] == 1
        assert by_name["_unmatched"]["count"] == 2
        assert {it["current_stage"] for it in by_name["_unmatched"]["items"]} == {
            "Blocked", "Awaiting QA",
        }


class TestLegacyNameMatching:
    """When a step has empty `matches`, the step's `name` itself is
    treated as the identifier — the C4 effective_matches fallback.
    Lets existing demo YAMLs work without surgery."""

    def test_step_with_empty_matches_buckets_by_name(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        steps = [
            {"name": "Draft", "wip": False, "matches": []},
            {"name": "Merged", "wip": False, "matches": []},
        ]
        items = [_item("Draft"), _item("Draft"), _item("Merged")]
        with TestClient(app) as client:
            r = _post(
                client, _payload(steps),
                mock_fetch=lambda **kw: {
                    "items": items, "stopped_by": "items_cap",
                    "window_to": "2026-04-30",
                },
            )
        body = r.json()
        by_name = {p["step_name"]: p for p in body["per_step"]}
        assert by_name["Draft"]["count"] == 2
        assert by_name["Merged"]["count"] == 1


class TestCache:
    def test_repeat_call_hits_cache(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        calls = []

        def fetch(**kw):
            calls.append(1)
            return {
                "items": [_item("WIP")],
                "stopped_by": "items_cap", "window_to": "2026-04-30",
            }

        payload = _payload([{"name": "WIP", "wip": True, "matches": [{"stage": "WIP"}]}])
        with TestClient(app) as client:
            _post(client, payload, mock_fetch=fetch)
            _post(client, payload)
        assert len(calls) == 1

    def test_force_busts_cache(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        calls = []
        def fetch(**kw):
            calls.append(1)
            return {
                "items": [],
                "stopped_by": "time_window", "window_to": "2026-04-30",
            }
        payload = _payload([{"name": "A", "wip": True, "matches": [{"stage": "A"}]}])
        with TestClient(app) as client:
            _post(client, payload, mock_fetch=fetch)
            r = client.post(
                "/api/internal/contracts/_dry-run?force=true",
                json=payload, headers={"X-Requested-With": "fetch"},
            )
            assert r.status_code == 200
        assert len(calls) == 2


class TestCapSemantics:
    """The 200-items-or-30-days cap is a request to the fetcher —
    it gets to choose which limit bites first. We pass the cap
    parameters through and surface whichever limit applied."""

    def test_cap_params_threaded_through(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        captured = {}
        def fetch(**kw):
            captured.update(kw)
            return {
                "items": [],
                "stopped_by": "items_cap", "window_to": "2026-04-30",
            }
        payload = _payload([{"name": "A", "wip": True, "matches": [{"stage": "A"}]}])
        payload["items_cap"] = 50
        with TestClient(app) as client:
            _post(client, payload, mock_fetch=fetch)
        assert captured.get("items_cap") == 50
        assert captured.get("since") == "2026-04-01"


class TestWizardUIHasDryRunSection:
    """The wizard renders the Dry-run disclosure panel under the
    Steps editor — a From-date input + Dry-run button + per-step
    table area."""

    def test_dry_run_panel_in_template(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = client.get("/admin/contracts/new").text
        # The Dry-run panel exists.
        assert "dry-run-panel" in html
        # The endpoint is referenced in the JS.
        assert "_dry-run" in html
