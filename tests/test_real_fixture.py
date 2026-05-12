"""Integration test against a fixture recorded from the live GitHub API.

This catches schema drift or query-shape mistakes that the synthetic
fixtures might miss. If the cache misses (e.g. PR_SEARCH_QUERY changed),
re-record with:

    uv run flowmetrics week --repo astral-sh/uv \\
        --start 2026-05-04 --stop 2026-05-10 \\
        --cache-dir tests/fixtures/cache
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from flowmetrics import flowmetrics_for_window, make_github_source
from flowmetrics.cache import CacheMiss

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "cache"
REPO = "astral-sh/uv"
START = date(2026, 5, 4)
STOP = date(2026, 5, 10)


@pytest.mark.skipif(
    not FIXTURE_DIR.exists() or not any(FIXTURE_DIR.glob("*.json")),
    reason="No recorded fixture; run the CLI once to record",
)
def test_flowmetrics_for_week_against_recorded_fixture():
    try:
        result = flowmetrics_for_window(make_github_source(REPO, cache_dir=FIXTURE_DIR, read_only=True), START, STOP)
    except CacheMiss as exc:
        pytest.fail(f"Fixture cache miss — PR_SEARCH_QUERY changed? Re-record. {exc}")

    # Stable, observable invariants — not hard-coded numbers, since the
    # exact recording can be refreshed at any time.
    assert result.pr_count > 0
    assert result.total_cycle.total_seconds() > 0
    assert 0.0 < result.portfolio_efficiency <= 1.0

    # At least one PR should be in the "fast merge" bucket (FE > 0.5)
    fast = [p for p in result.per_pr if p.efficiency > 0.5]
    assert fast, "expected at least one quick PR in a week of uv activity"

    # At least one PR should have meaningful wait time (FE < 0.2)
    slow = [p for p in result.per_pr if p.efficiency < 0.2]
    assert slow, "expected at least one slow PR — wait time is the point"
