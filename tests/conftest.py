"""Shared test fixtures.

The default `uv run pytest` invocation runs *unit* tests only. This
fixture blocks any network call from unit tests so the test suite can
run thousands of times without consuming GitHub API quota. Tests that
genuinely need the network must be marked `@pytest.mark.integration`.
"""

from __future__ import annotations

import subprocess

import httpx
import pytest


@pytest.fixture(autouse=True)
def _block_network_for_unit_tests(request, monkeypatch):
    """Replace httpx + `gh auth ...` with loud assertions for unit tests."""
    if "integration" in request.keywords:
        return  # integration tests get to use the network

    def _refuse_http(*args, **kwargs):
        raise AssertionError(
            "Unit test attempted a real network call. "
            "Mark with @pytest.mark.integration if a live API call is intentional, "
            "or wire an httpx.MockTransport / cache fixture to avoid it."
        )

    # Patch the real HTTP transports. httpx.MockTransport bypasses these,
    # so tests can still construct fake responses in-process.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _refuse_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _refuse_http)

    original_run = subprocess.run

    def _maybe_block_gh(args, *rest, **kwargs):
        # Block `gh auth ...` (token-fetch path). Leave everything else alone.
        if (
            isinstance(args, list | tuple)
            and len(args) >= 2
            and args[0] == "gh"
            and args[1] == "auth"
        ):
            raise AssertionError(
                "Unit test invoked `gh auth ...`. "
                "Mark with @pytest.mark.integration if intentional."
            )
        return original_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", _maybe_block_gh)
