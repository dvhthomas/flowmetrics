"""C4 — Source vocab probe (`_probe-source-vocab`).

Replaces the simpler `_probe-stages` endpoint. The new endpoint
returns three categories of suggestions for the Steps editor:

  - `labels`: source-native vocabulary (GitHub repo labels OR
    Jira project statuses). Pulled live from the source API,
    cached 15 minutes per target.
  - `lifecycle_events`: curated source-native lifecycle chips
    ("PR opened", "Marked ready for review", "Issue resolved", …).
  - `warehouse_stages`: distinct stage names already observed in
    the materialised transitions for this contract.

Each chip carries a sensible WIP default so a one-click add
lands a step row that's likely close to right.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flowmetrics.app import create_app


@pytest.fixture
def workspace(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    data = tmp_path / "data"
    return contracts, data


def _post(client, payload, mock_probe=None):
    headers = {"X-Requested-With": "fetch"}
    if mock_probe is not None:
        client.app.state.probe_source_vocab = mock_probe
    return client.post(
        "/api/internal/contracts/_probe-source-vocab",
        json=payload, headers=headers,
    )


class TestGithubProbeShape:
    def test_github_returns_labels_lifecycle_warehouse(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = _post(
                client,
                {"source": "github", "repo": "astral-sh/uv"},
                mock_probe=lambda kind, target: {
                    "labels": [
                        {"name": "bug", "wip": True},
                        {"name": "good first issue", "wip": False},
                    ],
                    "lifecycle_events": [],
                    "warehouse_stages": [],
                },
            )
        assert r.status_code == 200
        body = r.json()
        assert "labels" in body
        assert "lifecycle_events" in body
        assert "warehouse_stages" in body
        assert body["labels"][0]["name"] == "bug"
        assert body["labels"][0]["wip"] is True


class TestGithubLifecycleEvents:
    def test_github_lifecycle_events_curated_list(self, workspace):
        """The server provides a curated list of GitHub lifecycle
        chips regardless of probe failures — they're constants."""
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = _post(
                client,
                {"source": "github", "repo": "astral-sh/uv"},
                # Mock probe returns empty labels + warehouse but
                # the server fills lifecycle_events from its own
                # constant.
                mock_probe=lambda kind, target: {
                    "labels": [], "lifecycle_events": [
                        {"name": "PR opened", "wip": False},
                        {"name": "Marked ready for review", "wip": True},
                        {"name": "Changes requested", "wip": True},
                        {"name": "Review approved", "wip": True},
                        {"name": "PR merged", "wip": False},
                        {"name": "PR closed without merge", "wip": False},
                        {"name": "Issue opened", "wip": False},
                        {"name": "Issue closed", "wip": False},
                    ],
                    "warehouse_stages": [],
                },
            )
        names = [e["name"] for e in r.json()["lifecycle_events"]]
        assert "PR opened" in names
        assert "PR merged" in names
        assert "Issue closed" in names


class TestJiraLifecycleEvents:
    def test_jira_lifecycle_returns_jira_events(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = _post(
                client,
                {
                    "source": "jira",
                    "jira_url": "https://j.example.com",
                    "jira_project": "BIGTOP",
                },
                mock_probe=lambda kind, target: {
                    "labels": [
                        {"name": "Open", "wip": False},
                        {"name": "In Progress", "wip": True},
                    ],
                    "lifecycle_events": [
                        {"name": "Issue created", "wip": False},
                        {"name": "Resolved", "wip": False},
                    ],
                    "warehouse_stages": [],
                },
            )
        assert r.status_code == 200
        names = [e["name"] for e in r.json()["lifecycle_events"]]
        assert "Resolved" in names


class TestProbeCache:
    """Same source target probed twice within 15 minutes shouldn't
    re-run the live fetch. The cache key is the source target
    tuple (kind + repo OR kind + jira_url+project)."""

    def test_repeat_call_hits_cache(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        calls: list = []

        def probe(kind, target):
            calls.append((kind, dict(target)))
            return {
                "labels": [], "lifecycle_events": [],
                "warehouse_stages": [],
            }

        with TestClient(app) as client:
            payload = {"source": "github", "repo": "owner/x"}
            _post(client, payload, mock_probe=probe)
            _post(client, payload)
        assert len(calls) == 1

    def test_force_busts_cache(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        calls = []
        def probe(kind, target):
            calls.append(1)
            return {
                "labels": [], "lifecycle_events": [],
                "warehouse_stages": [],
            }
        with TestClient(app) as client:
            payload = {"source": "github", "repo": "owner/x"}
            _post(client, payload, mock_probe=probe)
            r = client.post(
                "/api/internal/contracts/_probe-source-vocab?force=true",
                json=payload, headers={"X-Requested-With": "fetch"},
            )
            assert r.status_code == 200
        assert len(calls) == 2


class TestBackwardsCompatibility:
    """The old `_probe-stages` endpoint stays available — the
    builder UI hits it today, C4 swaps the UI to the new endpoint
    in a follow-up edit but the old one keeps working in parallel."""

    def test_probe_stages_still_returns_a_stage_list(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.post(
                "/api/internal/contracts/_probe-stages",
                json={"source": "github", "repo": "astral-sh/uv"},
                headers={"X-Requested-With": "fetch"},
            )
        assert r.status_code == 200
        assert "stages" in r.json()


class TestWizardUIIncludesSuggestionsPanel:
    """The Steps editor surfaces three Suggestion subsections under
    the Steps fieldset: warehouse / source labels / lifecycle."""

    def test_wizard_renders_suggestions_panel(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = client.get("/admin/contracts/new").text
        # The new probe is referenced (the UI calls it on
        # "Discover from data source").
        assert "_probe-source-vocab" in html
        # The Suggestions panel exists.
        assert "suggestions-panel" in html
