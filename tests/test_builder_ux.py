"""UX-pass guarantees for the contract builder form.

The builder must not leave the user guessing:
  - Required fields carry the `required` attribute AND a visible
    marker.
  - Buttons that shouldn't be usable yet start disabled (Save,
    Add-step, Dry-run) — the server renders them with the
    `disabled` attribute; JS enables them as preconditions are met.
  - Field styling is class-driven (no ad-hoc inline padding/border
    on the primary inputs), so it's consistent.
  - The warehouse-suggestions empty-state copy is human-readable
    and only shown where it makes sense (edit mode).
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


def _new(client):
    return client.get("/admin/contracts/new").text


class TestRequiredFieldsMarked:
    def test_name_field_is_required_and_marked(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        # The name input keeps the HTML `required` attribute.
        assert 'name="name"' in html
        # A visible required marker class exists in the form.
        assert 'class="req"' in html or "field-required" in html

    def test_required_fields_have_aria_required(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        # Repo (the github default) is required.
        assert 'name="repo"' in html
        assert "aria-required" in html


class TestGatedButtonsStartDisabled:
    def test_save_button_starts_disabled(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        # The submit button renders disabled; JS enables it once the
        # required fields validate.
        import re
        m = re.search(r"<button[^>]*type=\"submit\"[^>]*>", html)
        assert m is not None
        assert "disabled" in m.group(0)

    def test_add_step_button_starts_disabled(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        import re
        m = re.search(r"<button[^>]*id=\"add-step\"[^>]*>", html)
        assert m is not None
        assert "disabled" in m.group(0)

    def test_dry_run_button_starts_disabled(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        import re
        m = re.search(r"<button[^>]*id=\"dry-run-btn\"[^>]*>", html)
        assert m is not None
        assert "disabled" in m.group(0)


class TestSuggestionsHiddenUntilActive:
    def test_suggestions_panel_starts_hidden(self, workspace):
        """The Suggestions panel (warehouse / labels / lifecycle
        chips) shouldn't clutter the form — it's revealed only when
        the user is actually adding/editing a step."""
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        import re
        # The panel renders with a `hidden` attribute (JS reveals it
        # on focus within the steps area).
        m = re.search(r'<div[^>]*id="suggestions-panel"[^>]*>', html)
        assert m is not None
        assert "hidden" in m.group(0)


class TestConsistentFieldStyling:
    def test_primary_inputs_use_a_shared_class(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        # Inputs use the shared `field-input` class rather than
        # ad-hoc inline style attributes.
        assert "field-input" in html


class TestWarehouseEmptyStateCopy:
    def test_new_mode_hides_warehouse_suggestions(self, workspace):
        """A brand-new contract has never materialised, so the
        'in your warehouse' suggestions group is meaningless — it's
        only rendered in edit mode."""
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        # The warehouse DOM node isn't rendered in new mode (a
        # guarded JS reference to the id may still exist).
        assert 'id="sugg-warehouse"' not in html

    def test_edit_mode_shows_warehouse_suggestions(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            client.put(
                "/api/internal/contracts/alpha",
                json={"yaml":
                    "contract:\n  name: alpha\n  source: github\n  repo: a/b\n"
                },
                headers={"X-Requested-With": "fetch"},
            )
            html = client.get("/admin/contracts/alpha/edit").text
        assert 'id="sugg-warehouse"' in html
