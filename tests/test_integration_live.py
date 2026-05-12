"""Live-API integration tests.

These hit the real GitHub GraphQL API and require either `gh auth login`
or `$GITHUB_TOKEN`. They are skipped by default — run them explicitly:

    uv run pytest -m integration

Keep them small and few: every run consumes GraphQL quota. We use a tiny
1-day window against an active public repo and a temporary cache dir so
nothing is shared with the recorded fixtures.
"""

from __future__ import annotations

from datetime import date

import pytest

from flowmetrics.cache import FileCache
from flowmetrics.github import GitHubClient, fetch_prs_merged_in_window

pytestmark = pytest.mark.integration


def test_one_day_live_fetch_against_astral_sh_uv(tmp_path):
    cache = FileCache(tmp_path)
    client = GitHubClient(cache, read_only=False)
    try:
        # 1-day window: smallest reasonable surface, lowest API cost.
        prs = fetch_prs_merged_in_window(
            client,
            "astral-sh/uv",
            date(2026, 5, 5),
            date(2026, 5, 5),
        )
    finally:
        client.close()
    # A typical day for uv has several merges. Assert only structural
    # invariants so the test stays stable across re-runs:
    assert isinstance(prs, list)
    if prs:
        first = prs[0]
        assert first.number > 0
        assert first.merged_at is not None
        assert first.created_at <= first.merged_at

    # Cache directory should now have at least one file — the recorded
    # response. Re-running the same query (read_only=True) must hit cache.
    cached_client = GitHubClient(cache, read_only=True)
    try:
        prs_again = fetch_prs_merged_in_window(
            cached_client,
            "astral-sh/uv",
            date(2026, 5, 5),
            date(2026, 5, 5),
        )
    finally:
        cached_client.close()
    assert prs_again == prs
