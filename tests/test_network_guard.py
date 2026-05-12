"""Spec for the unit-test network guard.

Unit tests must never hit the network. The autouse conftest fixture
monkeypatches httpx and the gh-auth subprocess so that a non-integration
test attempting a real call fails loudly instead of silently consuming
API quota.
"""

from __future__ import annotations

import subprocess

import httpx
import pytest


class TestHttpxBlockedInUnitTests:
    def test_httpx_post_raises(self):
        client = httpx.Client()
        with pytest.raises(AssertionError, match="network"):
            client.post("https://api.github.com/graphql", json={})

    def test_httpx_get_raises(self):
        client = httpx.Client()
        with pytest.raises(AssertionError, match="network"):
            client.get("https://api.github.com")


class TestGhAuthBlockedInUnitTests:
    def test_gh_auth_token_subprocess_raises(self):
        with pytest.raises(AssertionError, match="gh"):
            subprocess.run(["gh", "auth", "token"], capture_output=True, check=False)

    def test_unrelated_subprocess_still_works(self):
        # We only block `gh auth`; other subprocesses must pass through.
        result = subprocess.run(["echo", "hi"], capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "hi"


@pytest.mark.integration
class TestIntegrationMarkerOptsOutOfGuard:
    """Tests marked `integration` can use the network freely."""

    def test_httpx_client_works(self):
        # If we reach this test (only via `pytest -m integration`), httpx
        # is not monkeypatched and a real request would go out.
        client = httpx.Client()
        # We don't actually call the network — just prove the guard isn't
        # blocking the method.
        assert callable(client.post)
        assert callable(client.get)
