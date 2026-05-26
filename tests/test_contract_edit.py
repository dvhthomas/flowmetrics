"""Edit-existing-contract page — `/admin/contracts/{id}/edit`.

Reuses the wizard template with `mode: edit`. Pre-fills every field
from the existing YAML; save routes through PUT (idempotent
overwrite); delete routes through DELETE with a name-typing
confirmation in JS (server-side stays the same as B2).
"""

from __future__ import annotations

from pathlib import Path

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


def _write(contracts: Path, name: str, **fields) -> None:
    payload = {"name": name, "source": "github", "repo": "owner/repo"}
    payload.update(fields)
    (contracts / f"{name}.yaml").write_text(
        yaml.safe_dump({"contract": payload})
    )


class TestEditPage:
    def test_renders_at_admin_contracts_id_edit(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha", label="Alpha workflow")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.get("/admin/contracts/alpha/edit")
        assert r.status_code == 200
        html = r.text
        # Edit mode bakes the contract id into the page so the JS
        # knows to PUT this id and to skip the wizard's id field.
        assert "alpha" in html
        # The page hosts the same fieldsets as the new wizard.
        assert 'name="source"' in html
        # Delete affordance is on the edit page (only).
        assert "Delete" in html

    def test_unknown_id_returns_404(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.get("/admin/contracts/missing/edit")
        assert r.status_code == 404

    def test_dashboard_links_to_edit_for_existing_contract(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = client.get("/workflows/alpha").text
        # The data-source strip (or somewhere visible on the
        # dashboard) carries an Edit link to this contract.
        assert "/admin/contracts/alpha/edit" in html


class TestEditRoundTrip:
    """Editing label via PUT is already covered by B2; this confirms
    the GET endpoint that the edit page hydrates from carries every
    field needed for the form."""

    def test_get_payload_carries_every_form_field(self, workspace):
        contracts, data = workspace
        _write(
            contracts, "alpha",
            label="Alpha", start="2026-05-04", stop="2026-05-10",
        )
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            body = client.get("/api/internal/contracts/alpha").json()
        parsed = body["parsed"]
        for field in ("name", "source", "repo", "label", "start", "stop"):
            assert field in parsed, f"missing {field} from parsed payload"
        # Raw YAML present for the textarea fallback / diff view.
        assert "alpha" in body["yaml"]

    def test_get_payload_carries_states_when_present(self, workspace):
        contracts, data = workspace
        (contracts / "alpha.yaml").write_text(yaml.safe_dump({
            "contract": {
                "name": "alpha", "source": "github", "repo": "a/b",
                "states": {
                    "wip": ["Draft", "Awaiting Review"],
                    "done": ["Merged"],
                },
            }
        }))
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            body = client.get("/api/internal/contracts/alpha").json()
        assert body["parsed"]["states"] == {
            "backlog": [], "wip": ["Draft", "Awaiting Review"],
            "done": ["Merged"],
        }
