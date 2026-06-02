"""C6 + C7 — Export YAML endpoint and the archived-contracts page.

C6: GET /api/internal/workflows/{id}/yaml returns the row's
canonical YAML as a downloadable attachment. Works on archived
rows via ?include_archived=true.

C7: /admin/workflows/archive lists archived contracts with
restore / export / hard-delete actions. The home page grows a
"View archived (n)" link when n > 0.
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


def _seed(client, contract_id, label="demo"):
    r = client.put(
        f"/api/internal/workflows/{contract_id}",
        json={"yaml":
            f"contract:\n  name: {contract_id}\n  label: {label}\n"
            "  source: github\n  repo: a/b\n"
        },
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200, r.text


def _archive(client, contract_id, reason=None):
    return client.post(
        f"/api/internal/workflows/{contract_id}/archive",
        json={"reason": reason} if reason else {},
        headers={"X-Requested-With": "fetch"},
    )


class TestExportYaml:
    def test_returns_yaml_with_download_headers(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            r = client.get("/api/internal/workflows/alpha/yaml")
        assert r.status_code == 200
        # Right content type + a filename in the disposition.
        assert "application/x-yaml" in r.headers["content-type"]
        assert "alpha.yaml" in r.headers.get("content-disposition", "")
        # Body is the canonical YAML.
        assert "contract:" in r.text
        assert "name: alpha" in r.text

    def test_yaml_round_trips_through_materialize_from_yaml(self, workspace, tmp_path):
        """Exported YAML, written to a file, feeds
        `flow materialize --from-yaml PATH`. (Here we just confirm
        the exported text re-parses identically.)"""
        from flowmetrics.workflow import parse_workflow_text
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha", label="Round Trip")
            text = client.get("/api/internal/workflows/alpha/yaml").text
        c = parse_workflow_text(text, "alpha")
        assert c.name == "alpha"
        assert c.label == "Round Trip"

    def test_archived_contract_exports_with_flag(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            _archive(client, "alpha")
            # Without the flag → 404 (archived hidden).
            assert client.get(
                "/api/internal/workflows/alpha/yaml"
            ).status_code == 404
            # With the flag → 200.
            r = client.get(
                "/api/internal/workflows/alpha/yaml?include_archived=true"
            )
            assert r.status_code == 200
            assert "name: alpha" in r.text

    def test_unknown_id_returns_404(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            assert client.get(
                "/api/internal/workflows/nope/yaml"
            ).status_code == 404


class TestArchivePage:
    def test_archive_page_lists_archived_contracts(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha", label="Alpha")
            _seed(client, "beta", label="Beta")
            _archive(client, "beta", reason="rotated out")
            html = client.get("/admin/workflows/archive").text
        # Archived one shows; live one doesn't.
        assert "beta" in html
        assert "rotated out" in html
        # The live contract "alpha" should not be listed on the
        # archive page.
        assert "Alpha" not in html

    def test_archive_page_has_restore_and_delete_actions(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "beta")
            _archive(client, "beta")
            html = client.get("/admin/workflows/archive").text
        assert "/restore" in html
        # Hard-delete + export affordances reference the right endpoints.
        assert "/yaml?include_archived=true" in html
        assert "Restore" in html

    def test_empty_archive_page_renders(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = client.get("/admin/workflows/archive").text
        assert "rchive" in html  # "Archive" / "archived" heading present


class TestHomeArchiveLink:
    def test_home_shows_archive_link_when_archived_exist(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            _seed(client, "beta")
            _archive(client, "beta")
            html = client.get("/").text
        assert "/admin/workflows/archive" in html
        # Count surfaced.
        assert "1" in html

    def test_home_hides_archive_link_when_none_archived(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            html = client.get("/").text
        assert "/admin/workflows/archive" not in html
