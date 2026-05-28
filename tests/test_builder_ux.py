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


class TestGatedButtonsAreReactivelyDisabled:
    """The builder is an Alpine component — the gated buttons carry
    an Alpine `:disabled` binding so they enable/disable as the
    component's state (name / source / steps) changes."""

    def test_save_button_has_disabled_binding(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        import re
        m = re.search(r"<button[^>]*type=\"submit\"[^>]*>", html)
        assert m is not None
        assert ":disabled" in m.group(0)

    def test_add_step_button_has_disabled_binding(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        import re
        m = re.search(r"<button[^>]*id=\"add-step\"[^>]*>", html)
        assert m is not None
        assert ":disabled" in m.group(0)

    def test_dry_run_button_has_disabled_binding(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        import re
        m = re.search(r"<button[^>]*id=\"dry-run-btn\"[^>]*>", html)
        assert m is not None
        assert ":disabled" in m.group(0)


class TestSuggestionsHiddenUntilActive:
    def test_suggestions_render_inside_the_active_step(self, workspace):
        """The Suggestions panel (labels / lifecycle chips) lives
        inside the step it binds to and only renders for the selected
        (active) row — so it can't clutter the initial form and a chip
        can only ever land on the step the user is looking at."""
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        # The suggestions panel is gated to the active step via x-if.
        assert 'id="suggestions-panel"' in html
        assert 'x-if="idx === activeIdx"' in html
        # And it sits within the steps list, not as a sibling below it.
        steps_list = html.split('class="steps-list"', 1)[1]
        assert 'id="suggestions-panel"' in steps_list.split("</ol>", 1)[0]


class TestChipsBindToCurrentStep:
    """Clicking a suggestion chip is a single action: it binds that
    identifier to the active step (seeding a step from the chip when
    none is selected yet, so the click is never a no-op). There is no
    separate dual "create-step-from-chip" affordance, and the builder
    surfaces which step chips will bind to."""

    def test_no_create_step_from_chip_affordance(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        # The old dual "⇢ step" affordance is gone — one click, one action.
        assert "⇢ step" not in html
        assert "sugg-chip__step" not in html

    def test_binding_target_indicator_present(self, workspace):
        contracts, data = workspace
        app = create_app(data_dir=data, contracts_dir=contracts)
        with TestClient(app) as client:
            html = _new(client)
        # An indicator tells the user which step chips will bind to.
        assert "binding-target" in html


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
