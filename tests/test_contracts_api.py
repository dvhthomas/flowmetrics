"""Contract read API for the future web-UI builder.

Two endpoints, both intentionally simple:

  GET /api/internal/contracts
      → [{id, label, source}]  for every YAML in --workflows-dir

  GET /api/internal/contracts/{id}
      → {id, label, parsed: {...}, yaml: STRING,
         materialise: {last_run_at, status, items}}

These are the read foundation that the write API (B2) and the
contract-builder UI (B3..B5) layer on. Auth posture matches the rest
of the dashboard: open on 127.0.0.1, HTTP Basic when bound off-host.
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


class TestListContracts:
    def test_returns_every_yaml_under_workflows_dir(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha", label="Alpha workflow")
        _write(contracts, "beta", source="jira",
               jira_url="https://issues.apache.org/jira",
               jira_project="BIGTOP", repo=None)
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.get("/api/internal/contracts")
        assert r.status_code == 200
        body = r.json()
        # The shape is documented: id + label + source per entry.
        by_id = {c["id"]: c for c in body}
        assert by_id["alpha"]["label"] == "Alpha workflow"
        assert by_id["alpha"]["source"] == "github"
        # Label falls back to id when not explicitly set.
        assert by_id["beta"]["source"] == "jira"

    def test_empty_workflows_dir_returns_an_empty_list(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.get("/api/internal/contracts")
        assert r.status_code == 200
        assert r.json() == []

    def test_ignores_yml_yaml_alongside_each_other_consistently(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha")
        # A .yml twin should also appear — load_contract accepts both.
        (contracts / "gamma.yml").write_text(
            yaml.safe_dump({"contract": {
                "name": "gamma", "source": "github", "repo": "a/b",
            }})
        )
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            ids = {c["id"] for c in client.get("/api/internal/contracts").json()}
        assert ids == {"alpha", "gamma"}


class TestGetContractDetail:
    def test_returns_parsed_fields_plus_raw_yaml(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha", label="Alpha workflow")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.get("/api/internal/contracts/alpha")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "alpha"
        assert body["label"] == "Alpha workflow"
        # Parsed view that the UI can consume without re-parsing YAML.
        assert body["parsed"]["source"] == "github"
        assert body["parsed"]["repo"] == "owner/repo"
        # The original YAML text — for the textarea fallback / diffing.
        assert "contract:" in body["yaml"]
        assert "alpha" in body["yaml"]

    def test_includes_materialise_status_block(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            body = client.get("/api/internal/contracts/alpha").json()
        # Always present; null when no run has happened yet.
        assert "materialise" in body
        m = body["materialise"]
        if m is not None:
            assert isinstance(m.get("last_run_at"), (str, type(None)))
            assert m.get("status") in (None, "ok", "done", "running", "failed")

    def test_unknown_id_returns_404(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.get("/api/internal/contracts/does-not-exist")
        assert r.status_code == 404

    def test_malformed_yaml_returns_422_with_parser_message(self, workspace):
        contracts, data = workspace
        # Missing `source:` → ContractError at load time.
        (contracts / "broken.yaml").write_text("contract: {name: broken}\n")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.get("/api/internal/contracts/broken")
        # Validation failures get 422; the listing still returns the
        # entry, but detail surfaces the parser error.
        assert r.status_code == 422
        assert "source" in r.text.lower() or "contract" in r.text.lower()


class TestAuthPosture:
    def test_no_auth_required_on_localhost(self, workspace):
        # The factory's `password` arg is the off-host gate; when None,
        # all routes are open. (TestClient bypasses the host check.)
        contracts, data = workspace
        _write(contracts, "alpha")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            assert client.get("/api/internal/contracts").status_code == 200
            assert client.get("/api/internal/contracts/alpha").status_code == 200

    def test_password_set_requires_basic_auth(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha")
        app = create_app(
            data_dir=data, contracts_dir=contracts, password="secret",
        )
        with TestClient(app) as client:
            unauth = client.get("/api/internal/contracts")
            assert unauth.status_code == 401
            authd = client.get(
                "/api/internal/contracts",
                auth=("operator", "secret"),
            )
            assert authd.status_code == 200
