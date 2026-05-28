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

    def test_malformed_yaml_is_skipped_by_migration_and_returns_404(self, workspace):
        contracts, data = workspace
        # Missing `source:` → ContractError on parse. The migration
        # leaves the bad YAML in place (so the user can see + fix
        # it) and does NOT create a DB row. The detail endpoint
        # therefore returns 404 rather than 422.
        (contracts / "broken.yaml").write_text("contract: {name: broken}\n")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.get("/api/internal/contracts/broken")
        assert r.status_code == 404
        # The original file is still on disk for the user to fix.
        assert (contracts / "broken.yaml").exists()


class TestValidateEndpoint:
    """`POST /api/internal/contracts/_validate` is the structured
    parser the UI calls live to surface errors without touching disk.
    Returns the same outcome `load_contract` would, but as JSON the
    UI can render inline."""

    def _post(self, client, **kwargs):
        # Writes require the "this came from our UI" header so a
        # drive-by cross-origin POST can't forge them.
        kwargs.setdefault("headers", {})["X-Requested-With"] = "fetch"
        return client.post("/api/internal/contracts/_validate", **kwargs)

    def test_valid_yaml_returns_valid_true(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = self._post(client, json={"yaml":
                "contract:\n  name: alpha\n  source: github\n  repo: a/b\n"
            })
        assert r.status_code == 200
        body = r.json()
        assert body == {"valid": True, "errors": []}

    def test_invalid_yaml_returns_structured_errors(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        # Missing source: → ContractError.
        with TestClient(app) as client:
            r = self._post(client, json={"yaml":
                "contract:\n  name: alpha\n"
            })
        assert r.status_code == 200  # validate ALWAYS returns 200
        body = r.json()
        assert body["valid"] is False
        assert body["errors"]
        # Each error names a message; line/column may be null when
        # semantic (the parser can't pin a row).
        for e in body["errors"]:
            assert "message" in e

    def test_yaml_syntax_error_pins_line_and_column(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        # Bad indentation → PyYAML raises with a problem_mark.
        with TestClient(app) as client:
            r = self._post(client, json={"yaml":
                "contract:\n  name: alpha\n source: github\n"
            })
        body = r.json()
        assert body["valid"] is False
        assert any(
            e.get("line") is not None and e.get("line") > 0
            for e in body["errors"]
        )


class TestWriteContract:
    def _put(self, client, contract_id, yaml_text):
        return client.put(
            f"/api/internal/contracts/{contract_id}",
            json={"yaml": yaml_text},
            headers={"X-Requested-With": "fetch"},
        )

    def test_creates_a_new_contract(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = self._put(client, "alpha",
                "contract:\n  name: alpha\n  source: github\n  repo: a/b\n")
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["id"] == "alpha"
            # The detail endpoint sees it immediately.
            assert client.get("/api/internal/contracts/alpha").status_code == 200

    def test_overwrites_an_existing_contract_atomically(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha", label="old")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = self._put(client, "alpha",
                "contract:\n  name: alpha\n  label: new\n  source: github\n  repo: a/b\n")
            assert r.status_code == 200, r.text
            # New label visible through the API (DB-backed).
            body = client.get("/api/internal/contracts/alpha").json()
        assert body["parsed"]["label"] == "new"

    def test_invalid_yaml_rejects_with_422_and_does_not_write(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = self._put(client, "broken", "contract: {name: broken}\n")
            assert r.status_code == 422
            body = r.json()
            assert "errors" in body or "detail" in body
            # Nothing stored.
            assert client.get("/api/internal/contracts/broken").status_code == 404

    def test_id_must_match_contract_name(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            # URL says "alpha" but YAML says "beta" → reject.
            r = self._put(client, "alpha",
                "contract:\n  name: beta\n  source: github\n  repo: a/b\n")
            assert r.status_code == 422
            assert client.get("/api/internal/contracts/alpha").status_code == 404
            assert client.get("/api/internal/contracts/beta").status_code == 404


class TestDeleteContract:
    def _delete(self, client, contract_id, **kwargs):
        kwargs.setdefault("headers", {})["X-Requested-With"] = "fetch"
        return client.delete(
            f"/api/internal/contracts/{contract_id}", **kwargs
        )

    def test_removes_yaml_when_no_warehouse_data(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = self._delete(client, "alpha")
            assert r.status_code == 200, r.text
            assert client.get("/api/internal/contracts/alpha").status_code == 404

    def test_refuses_when_warehouse_data_exists(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha")
        # Synthesise a Parquet leaf for this contract.
        work = (
            data / "work_items" / "contract_id=alpha"
            / "year=2026" / "month=05" / "day=10"
        )
        work.mkdir(parents=True)
        (work / "items-r1.parquet").write_bytes(b"fake")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = self._delete(client, "alpha")
            assert r.status_code == 409
            body = r.json()
            assert "purge" in str(body).lower()
            # Contract still in the DB; warehouse still on disk.
            assert client.get("/api/internal/contracts/alpha").status_code == 200
        assert work.exists()

    def test_purge_data_true_clears_warehouse_alongside_yaml(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha")
        work = (
            data / "work_items" / "contract_id=alpha"
            / "year=2026" / "month=05" / "day=10"
        )
        work.mkdir(parents=True)
        (work / "items-r1.parquet").write_bytes(b"fake")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.request(
                "DELETE",
                "/api/internal/contracts/alpha",
                json={"purge_data": True},
                headers={"X-Requested-With": "fetch"},
            )
            assert r.status_code == 200, r.text
            assert client.get("/api/internal/contracts/alpha").status_code == 404
        # The contract's partition is gone; other partitions would
        # be untouched (there are none in this test).
        assert not (data / "work_items" / "contract_id=alpha").exists()


class TestCSRFOnWrites:
    """Writes require X-Requested-With: fetch. A drive-by POST from a
    malicious page can't set that header cross-origin without
    triggering a preflight (which we don't accept), so the header's
    presence is enough proof the request came from our own UI."""

    def test_put_without_header_is_blocked(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.put(
                "/api/internal/contracts/alpha",
                json={"yaml":
                    "contract:\n  name: alpha\n  source: github\n  repo: a/b\n"
                },
            )
            assert r.status_code == 403
            assert client.get("/api/internal/contracts/alpha").status_code == 404

    def test_delete_without_header_is_blocked(self, workspace):
        contracts, data = workspace
        _write(contracts, "alpha")
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.delete("/api/internal/contracts/alpha")
            assert r.status_code == 403
            # Still in the DB after the blocked DELETE.
            assert client.get("/api/internal/contracts/alpha").status_code == 200

    def test_validate_without_header_is_also_blocked(self, workspace):
        # _validate is a POST that takes user input — same CSRF
        # surface as PUT/DELETE.
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = client.post(
                "/api/internal/contracts/_validate",
                json={"yaml": "x: y"},
            )
            assert r.status_code == 403


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
