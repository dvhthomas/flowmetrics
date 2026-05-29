"""The home workflow list must let you *manage* a config, not just open
its dashboard. Each row carries an Edit link into the builder — where
Archive (reversible soft-delete) lives, and from the archived list,
permanent delete. Without this the only path to delete is buried behind
a pencil icon on the dashboard's data-source strip ("there's no way to
delete a config")."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from flowmetrics.app import create_app


@pytest.fixture
def client(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    app = create_app(data_dir=tmp_path / "data", contracts_dir=contracts)
    with TestClient(app) as c:
        c.put(
            "/api/internal/contracts/alpha",
            json={"yaml": "contract:\n  name: alpha\n  source: github\n  repo: a/b\n"},
            headers={"X-Requested-With": "fetch"},
        )
        yield c


def test_home_row_has_an_edit_link_per_workflow(client):
    html = client.get("/").text
    assert "/admin/contracts/alpha/edit" in html
