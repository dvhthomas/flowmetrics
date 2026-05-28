"""E2E: the contract builder's Steps editor (Alpine component).

Drives the real Alpine component in Chromium with the source probes
stubbed via `app.state` — no network. Pins the chip-binding contract:

  - Suggestion chips live *inside* the step they bind to. Only the
    selected (green) row renders them, so a chip can only ever land on
    the step the user is looking at — no phantom steps, no ambiguity.
  - Selecting another step row retargets the chips to that step.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port,
            log_level="error", access_log=False,
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    from flowmetrics.app import create_app

    tmp = tmp_path_factory.mktemp("builder-steps-e2e")
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"

    app = create_app(data_dir=data_dir, contracts_dir=contracts_dir)
    # Stub the probes so the builder verifies the source + loads label
    # chips without touching the network.
    app.state.probe_source = lambda source, target: {
        "ok": True, "label": "stub repo",
    }
    app.state.probe_source_vocab = lambda source, target: {
        "labels": [{"name": "ready"}, {"name": "in-review"}],
        "lifecycle_events": [{"name": "Issue opened", "wip": False}],
        "warehouse_stages": [],
    }

    port = _free_port()
    thread = _ServerThread(app, port)
    thread.start()
    for _ in range(50):
        with (
            contextlib.suppress(OSError),
            socket.create_connection(("127.0.0.1", port), timeout=0.2),
        ):
            break
        time.sleep(0.1)
    else:
        thread.stop()
        raise RuntimeError("uvicorn did not start in time")
    yield f"http://127.0.0.1:{port}"
    thread.stop()
    thread.join(timeout=3)


def _verify_source(page: Page) -> None:
    page.fill("#f-repo", "owner/name")
    page.eval_on_selector("#f-repo", "el => el.blur()")
    page.wait_for_selector(".probe-status--ok", timeout=8000)
    page.wait_for_selector("#add-step:visible", timeout=4000)


def _add_step(page: Page, name: str) -> None:
    page.fill("#new-step-name", name)
    page.click("#add-step")
    page.wait_for_function(
        "n => [...document.querySelectorAll('#steps-list .step-name-input')]"
        ".some(i => i.value === n)",
        arg=name,
        timeout=4000,
    )


class TestChipsLiveInsideActiveStep:
    def test_no_chips_before_a_step_exists(
        self, server_url: str, page: Page
    ):
        """With a verified source but no step yet, there's no step to
        bind to — so no suggestion chips are rendered anywhere."""
        page.goto(server_url + "/admin/contracts/new")
        _verify_source(page)
        assert page.query_selector(".sugg-chip") is None

    def test_chip_binds_to_the_step_it_sits_in(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/admin/contracts/new")
        _verify_source(page)
        _add_step(page, "Ready")

        # The active row now renders its own suggestion chips...
        chip = page.wait_for_selector(
            "#steps-list .step-row #sugg-labels .sugg-chip >> text=ready",
            timeout=4000,
        )
        # ...and the suggestions panel is a descendant of the step row.
        inside = page.eval_on_selector(
            "#suggestions-panel", "el => !!el.closest('.step-row')"
        )
        assert inside, "suggestions must live inside the step row"

        chip.click()
        page.wait_for_timeout(300)
        rows = page.query_selector_all("#steps-list .step-row")
        assert len(rows) == 1, f"a chip must not spawn a step, got {len(rows)}"
        assert rows[0].query_selector(".step-name-input").input_value() == "Ready"
        pills = rows[0].query_selector_all(".match-pill")
        assert len(pills) == 1 and "ready" in pills[0].inner_text()

    def test_selecting_a_row_retargets_the_chips(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/admin/contracts/new")
        _verify_source(page)
        _add_step(page, "Ready")
        _add_step(page, "Done")  # this one is now active

        # Select the first row; its chips appear, the second row's vanish.
        rows = page.query_selector_all("#steps-list .step-row")
        rows[0].click()
        page.wait_for_function(
            "() => { const p = document.querySelector('#suggestions-panel');"
            "return p && p.closest('.step-row')"
            "  .querySelector('.step-name-input').value === 'Ready'; }",
            timeout=4000,
        )
        page.click(
            "#steps-list .step-row #sugg-labels .sugg-chip >> text=in-review"
        )
        page.wait_for_timeout(300)

        rows = page.query_selector_all("#steps-list .step-row")
        ready_pills = [p.inner_text() for p in rows[0].query_selector_all(".match-pill")]
        done_pills = [p.inner_text() for p in rows[1].query_selector_all(".match-pill")]
        assert any("in-review" in t for t in ready_pills), ready_pills
        assert not any("in-review" in t for t in done_pills), done_pills


class TestAddStepIsVisuallyDistinct:
    def test_add_step_control_is_labelled(
        self, server_url: str, page: Page
    ):
        """The add-step control must not read as just another step
        row — it carries a distinguishing label so users can tell the
        'create a step' field apart from committed steps."""
        page.goto(server_url + "/admin/contracts/new")
        _verify_source(page)
        expect(page.locator(".add-step-panel")).to_be_visible()
        expect(page.locator(".add-step-panel")).to_contain_text("Add a step")
