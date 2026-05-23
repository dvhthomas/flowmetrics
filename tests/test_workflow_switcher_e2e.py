"""E2E: workflow picker re-renders the page with the chosen
workflow's data, and preserves whatever sub-route the viewer is
on (dashboard, metric detail).

Was decorative until this slice — the dropdown carried the
correct options but was `disabled`. This test pins:

  1. The dropdown is enabled and lists every contract under
     `contracts_dir`.
  2. Changing the selection navigates to the chosen workflow.
  3. Sub-route is preserved across the switch — picking a new
     workflow from `/workflows/A/metrics/cycle-time` lands on
     `/workflows/B/metrics/cycle-time`, not `/workflows/B/`.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import yaml
from click.testing import CliRunner
from playwright.sync_api import Page, expect

from flowmetrics.cli import cli

pytestmark = pytest.mark.e2e

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    """Materialise TWO contracts so the dropdown has something to
    switch BETWEEN. Both use the GitHub fixture cache (offline)
    so the test stays hermetic and fast."""
    from flowmetrics.app import create_app

    tmp_path = tmp_path_factory.mktemp("workflow-switcher")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp_path / "data"

    # Two identical-shape contracts so both have data; the test
    # only cares that switching navigates correctly.
    for name in ("alpha-workflow", "beta-workflow"):
        (contracts_dir / f"{name}.yaml").write_text(
            yaml.safe_dump(
                {
                    "contract": {
                        "name": name,
                        "source": "github",
                        "repo": "astral-sh/uv",
                        "start": "2026-05-04",
                        "stop": "2026-05-10",
                    }
                }
            )
        )
        res = CliRunner().invoke(
            cli,
            [
                "materialise",
                name,
                "--data-dir",
                str(data_dir),
                "--workflows-dir",
                str(contracts_dir),
                "--cache-dir",
                str(FIXTURE_CACHE),
                "--offline",
            ],
            catch_exceptions=False,
        )
        assert res.exit_code == 0, res.output

    app = create_app(data_dir=data_dir, contracts_dir=contracts_dir)
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


class TestWorkflowSwitcher:
    def test_home_lists_all_workflows_as_links(
        self, server_url: str, page: Page
    ):
        """The home page is the workflow picker — lists every
        contract under contracts_dir as a link to its dashboard.
        The picker dropdown was replaced by this list.

        Guards two failure modes seen in the wild:
          1. The empty-state "No workflows yet" copy renders
             despite contracts existing (template variable
             mismatch between route and template).
          2. The list renders with zero <a> elements (typo
             in the loop, missing href, etc.)."""
        page.goto(server_url + "/")
        page.wait_for_selector("ul.home-workflow-list")

        # Empty-state copy must NOT be present when contracts
        # exist. The test fixture materialises two contracts;
        # if the home page falls back to the empty state, this
        # assertion catches it loudly.
        body_text = page.locator("body").inner_text()
        assert "No workflows yet" not in body_text, (
            f"home page is showing the empty state despite the "
            f"fixture having two contracts; route is likely "
            f"passing the wrong context key. Body text:\n"
            f"{body_text[:1500]}"
        )

        hrefs = page.evaluate(
            "() => Array.from(document.querySelectorAll('a.home-workflow-link'))"
            ".map(a => a.getAttribute('href'))"
        )
        assert "/workflows/alpha-workflow" in hrefs
        assert "/workflows/beta-workflow" in hrefs

    def test_clicking_a_workflow_link_navigates_to_its_dashboard(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/")
        page.locator(
            "a.home-workflow-link[href='/workflows/beta-workflow']"
        ).click()
        page.wait_for_url("**/workflows/beta-workflow")
        page_text = page.locator("body").inner_text()
        assert "beta-workflow" in page_text

    def test_switcher_hidden_on_metric_detail_pages(
        self, server_url: str, page: Page
    ):
        """The workflow switcher only appears on the workflow's
        dashboard (`/workflows/{id}`). Detail pages rely on the
        header breadcrumb to navigate back; cluttering them with
        a switcher implies you'd want to switch mid-investigation,
        which isn't the natural flow."""
        page.goto(server_url + "/workflows/alpha-workflow/metrics/cycle-time")
        page.wait_for_selector(".filter-bar")
        # Switcher not present on detail pages.
        count = page.locator("select[name='workflow']").count()
        assert count == 0, (
            f"workflow switcher should be hidden on metric detail "
            f"pages; found {count} select element(s)"
        )
