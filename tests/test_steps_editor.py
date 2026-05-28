"""C3 — Steps editor replaces the three-bucket Stages UI.

The v1 wizard's `data-bucket="backlog|wip|done"` buckets are gone.
The new editor is a single ordered list — each row is a step with
a name input and a WIP checkbox. The READY / WIP / DONE category
is derived from order + WIP flag (no separate bucket UI).

Tests pin:
  - The wizard template renders the Steps editor (a single
    ordered list, not three buckets).
  - The DOM carries hooks the JS uses to reorder + add + delete.
  - The "Discover from data source" button still wires to the
    `_probe-stages` endpoint (C4 will replace it with a richer
    source-vocab probe; for now the existing endpoint is fine).
  - Save round-trips a contract whose YAML carries the new
    `steps: [{name, wip}]` shape.
  - The edit page's Delete button calls /archive (the C2
    soft-delete path), not the hard DELETE.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from flowmetrics.app import create_app


@pytest.fixture
def workspace(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    data = tmp_path / "data"
    return contracts, data


def _seed(client, contract_id: str, steps: list[dict] | None = None) -> None:
    body = {
        "name": contract_id, "source": "github", "repo": "a/b",
    }
    if steps is not None:
        body["steps"] = steps
    text = yaml.safe_dump({"contract": body})
    r = client.put(
        f"/api/internal/contracts/{contract_id}",
        json={"yaml": text},
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200, r.text


class TestStepsEditorDom:
    def test_new_wizard_does_not_render_three_buckets(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = client.get("/admin/contracts/new").text
        # The old three-bucket markup MUST be gone.
        assert 'data-bucket="backlog"' not in html
        assert 'data-bucket="wip"' not in html
        assert 'data-bucket="done"' not in html
        # No "Backlog" / "WIP" / "Done" bucket strong-labels either.
        assert "stage-bucket-backlog" not in html
        assert "stage-bucket-wip" not in html
        assert "stage-bucket-done" not in html

    def test_renders_a_single_ordered_steps_list(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = client.get("/admin/contracts/new").text
        # The new editor's DOM hook.
        assert "steps-list" in html
        # The "+ Add step" affordance.
        assert "add-step" in html

    def test_discover_endpoint_is_wired(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = client.get("/admin/contracts/new").text
        # C4 swapped _probe-stages for the richer
        # _probe-source-vocab endpoint.
        assert "_probe-source-vocab" in html


class TestSaveRoundTrip:
    """Save flow: client builds the YAML, PUTs it, server stores
    the canonical `steps:` shape. GET returns it identically."""

    def test_steps_round_trip_through_put_and_get(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha", steps=[
                {"name": "Triage", "wip": False},
                {"name": "In Progress", "wip": True},
                {"name": "Review", "wip": True},
                {"name": "Done", "wip": False},
            ])
            detail = client.get("/api/internal/contracts/alpha").json()
        parsed_states = detail["parsed"].get("states")
        # The synthesised states compatibility shim:
        # leading non-WIP → backlog; contiguous WIP → wip; trailing
        # non-WIP → done.
        assert parsed_states["backlog"] == ["Triage"]
        assert parsed_states["wip"] == ["In Progress", "Review"]
        assert parsed_states["done"] == ["Done"]
        # The raw YAML stored uses the NEW `steps:` shape, not the
        # legacy `states:` block.
        assert "steps:" in detail["yaml"]
        assert "states:" not in detail["yaml"]


class TestEditPageDelete:
    """The edit page's Delete button must archive (C2 soft-delete),
    not call the hard-DELETE endpoint. The template carries the
    archive URL; the user can then hard-delete from the archived
    page later (C7) if they really want it gone."""

    def test_edit_page_references_archive_not_delete(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            html = client.get("/admin/contracts/alpha/edit").text
        # The archive endpoint is what the Delete button hits.
        assert "/archive" in html
        # Copy reads "Archive" rather than "Delete".
        assert "Archive" in html
