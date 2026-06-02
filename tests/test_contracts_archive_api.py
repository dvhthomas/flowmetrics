"""C2 — archive / restore / hard-delete lifecycle via the public API.

Endpoints:
  POST /api/internal/workflows/{id}/archive  — soft delete
  POST /api/internal/workflows/{id}/restore  — undo archive
  DELETE /api/internal/workflows/{id}        — hard delete; requires
                                                already-archived

List + detail:
  GET /api/internal/workflows?include_archived=true  — surfaces archived rows
  GET /api/internal/workflows/{id}                   — 404 by default on
                                                       archived; 200 with
                                                       ?include_archived=true

Materialize:
  `flow materialize-all` skips rows with archived_at set.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from flowmetrics.app import create_app


@pytest.fixture
def workspace(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    data = tmp_path / "data"
    return contracts, data


def _seed(client, contract_id: str, label: str = "demo") -> None:
    """Insert a workflow via the public API (the canonical path)."""
    r = client.put(
        f"/api/internal/workflows/{contract_id}",
        json={"yaml":
            f"workflow:\n  name: {contract_id}\n  label: {label}\n"
            "  source: github\n  repo: a/b\n"
        },
        headers={"X-Requested-With": "fetch"},
    )
    assert r.status_code == 200, r.text


def _archive(client, contract_id: str, reason: str | None = None):
    body = {"reason": reason} if reason is not None else {}
    return client.post(
        f"/api/internal/workflows/{contract_id}/archive",
        json=body, headers={"X-Requested-With": "fetch"},
    )


def _restore(client, contract_id: str):
    return client.post(
        f"/api/internal/workflows/{contract_id}/restore",
        headers={"X-Requested-With": "fetch"},
    )


def _delete(client, contract_id: str, **kwargs):
    kwargs.setdefault("headers", {})["X-Requested-With"] = "fetch"
    return client.delete(
        f"/api/internal/workflows/{contract_id}", **kwargs
    )


class TestArchiveEndpoint:
    def test_archive_soft_deletes(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            r = _archive(client, "alpha", reason="rotating out")
            assert r.status_code == 200, r.text
            # Default list excludes archived.
            assert client.get("/api/internal/workflows").json() == []
            # Detail returns 404 by default.
            assert client.get("/api/internal/workflows/alpha").status_code == 404

    def test_include_archived_surfaces_the_row(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            _archive(client, "alpha", reason="rotating out")
            r = client.get(
                "/api/internal/workflows?include_archived=true"
            )
            assert r.status_code == 200
            body = r.json()
            assert len(body) == 1
            assert body[0]["id"] == "alpha"
            assert body[0]["archived"] is True
            # Detail also accessible with the flag.
            detail = client.get(
                "/api/internal/workflows/alpha?include_archived=true"
            ).json()
            assert detail["archived"] is True
            assert detail["archived_reason"] == "rotating out"

    def test_archive_is_idempotent(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            _archive(client, "alpha", reason="first")
            r = _archive(client, "alpha", reason="second")
            assert r.status_code == 200
            detail = client.get(
                "/api/internal/workflows/alpha?include_archived=true"
            ).json()
            # Reason rolls forward; archive timestamp stayed put
            # (we can't easily compare floor timestamps in this
            # test, but the workflow carries the latest reason).
            assert detail["archived_reason"] == "second"


class TestRestoreEndpoint:
    def test_restore_unsoft_deletes(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            _archive(client, "alpha")
            r = _restore(client, "alpha")
            assert r.status_code == 200, r.text
            # Back in the live list.
            assert [c["id"] for c in client.get(
                "/api/internal/workflows"
            ).json()] == ["alpha"]

    def test_restore_on_live_row_is_a_no_op(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            # Restoring an already-live row succeeds without error.
            r = _restore(client, "alpha")
            assert r.status_code == 200, r.text

    def test_restore_unknown_id_returns_404(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            r = _restore(client, "nope")
            assert r.status_code == 404


class TestHardDeleteInvariant:
    """DELETE on a LIVE row must refuse; the user has to archive
    first. Prevents accidental erasure."""

    def test_live_delete_refuses_with_409_and_hint(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            r = _delete(client, "alpha")
            assert r.status_code == 409, r.text
            assert "archive" in r.text.lower()
            # Still there.
            assert client.get(
                "/api/internal/workflows/alpha"
            ).status_code == 200

    def test_delete_after_archive_succeeds(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            _archive(client, "alpha")
            r = _delete(client, "alpha")
            assert r.status_code == 200, r.text
            # Not visible even with include_archived.
            body = client.get(
                "/api/internal/workflows?include_archived=true"
            ).json()
            assert body == []

    def test_delete_with_purge_data_still_requires_archived(self, workspace):
        """purge_data is the warehouse-wiping flag. It DOES NOT
        bypass the archive-first invariant."""
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            _seed(client, "alpha")
            r = _delete(client, "alpha", params={"purge_data": "true"})
            assert r.status_code == 409


class TestArchivedExclusionFromMaterialize:
    """`flow materialize-all` must skip archived contracts so the
    cron path doesn't accidentally re-import data for retired
    workflows."""

    def test_materialize_all_skips_archived(self, workspace, tmp_path):
        from click.testing import CliRunner

        from flowmetrics.cli import cli
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            # Two contracts; one will be archived.
            _seed(client, "live-one")
            _seed(client, "to-archive")
            _archive(client, "to-archive")
        # materialize-all reads from the DB at this point.
        cache_dir = (
            Path(__file__).parent / "fixtures" / "cache"
        )
        CliRunner().invoke(cli, [
            "materialize", "--all",
            "--workflows-dir", str(contracts),
            "--data-dir", str(data),
            "--cache-dir", str(cache_dir),
            "--offline",
        ], catch_exceptions=False)
        # We don't care if the live-one workflow's fetch succeeds
        # in offline mode (the demo fixture may not match a/b);
        # we only care that the manifest's `results` does NOT
        # include the archived entry.
        manifest_path = next((data / "_status").glob("daily-*.json"))
        import json
        m = json.loads(manifest_path.read_text())
        ids_run = {r["workflow"] for r in m["results"]}
        assert "to-archive" not in ids_run
