"""Behavioural spec for the pure helpers in scripts/generate_samples.py.

The orchestration that calls the CLI and writes files is integration
territory — exercised manually. The pure parts (repo config, index
template, README rewrite) are testable.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# scripts/ isn't on the package path; add it for the test.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from generate_samples import (
    REPOS,
    SAMPLES_BEGIN,
    SAMPLES_END,
    Repo,
    SampleSet,
    build_index_html,
    rewrite_readme_samples_section,
)

# ---------------------------------------------------------------------------
# Repo configuration
# ---------------------------------------------------------------------------


class TestRepoConfig:
    def test_calcmark_go_calcmark_included(self):
        slugs = [r.slug for r in REPOS]
        assert "CalcMark/go-calcmark" in slugs

    def test_each_repo_has_archetype_label(self):
        for r in REPOS:
            assert r.slug
            assert r.archetype
            assert "/" in r.slug, f"slug should be owner/name: {r.slug}"

    def test_at_most_eight_repos_to_respect_api_quota(self):
        # Keep the runtime cost bounded. Cap covers 5 GitHub + a couple of
        # Jira projects without blowing through anyone's API budget.
        assert len(REPOS) <= 8

    def test_includes_at_least_one_jira_source(self):
        """Demo set advertises Jira parity — must include >=1 Jira entry."""
        assert any(r.cache_subdir == "jira" for r in REPOS)
        assert any("--jira-url" in r.cli_args for r in REPOS)


# ---------------------------------------------------------------------------
# Index page
# ---------------------------------------------------------------------------


def _sample_set(slug: str) -> SampleSet:
    return SampleSet(
        repo=Repo(slug=slug, archetype="test", cli_args=["--repo", slug]),
        efficiency_html=Path(f"samples/{slug.replace('/', '_')}/efficiency-week.html"),
        efficiency_json=Path(f"samples/{slug.replace('/', '_')}/efficiency-week.json"),
        efficiency_text=Path(f"samples/{slug.replace('/', '_')}/efficiency-week.txt"),
        when_done_html=Path(f"samples/{slug.replace('/', '_')}/forecast-when-done.html"),
        when_done_json=Path(f"samples/{slug.replace('/', '_')}/forecast-when-done.json"),
        when_done_text=Path(f"samples/{slug.replace('/', '_')}/forecast-when-done.txt"),
        how_many_html=Path(f"samples/{slug.replace('/', '_')}/forecast-how-many.html"),
        how_many_json=Path(f"samples/{slug.replace('/', '_')}/forecast-how-many.json"),
        how_many_text=Path(f"samples/{slug.replace('/', '_')}/forecast-how-many.txt"),
    )


class TestBuildIndexHtml:
    def test_is_complete_html_document(self):
        out = build_index_html(
            [_sample_set("astral-sh/uv")], datetime(2026, 5, 12, 14, 30, tzinfo=UTC)
        )
        assert "<!doctype html>" in out.lower()
        assert "</html>" in out

    def test_every_repo_appears(self):
        sets = [_sample_set("astral-sh/uv"), _sample_set("CalcMark/go-calcmark")]
        out = build_index_html(sets, datetime(2026, 5, 12, 14, 30, tzinfo=UTC))
        assert "astral-sh/uv" in out
        assert "CalcMark/go-calcmark" in out

    def test_links_to_every_format(self):
        sets = [_sample_set("astral-sh/uv")]
        out = build_index_html(sets, datetime(2026, 5, 12, 14, 30, tzinfo=UTC))
        assert "efficiency-week.html" in out
        assert "efficiency-week.json" in out
        assert "forecast-when-done.html" in out
        assert "forecast-how-many.html" in out

    def test_generated_at_rendered(self):
        out = build_index_html(
            [_sample_set("astral-sh/uv")], datetime(2026, 5, 12, 14, 30, 15, tzinfo=UTC)
        )
        assert "2026-05-12" in out


# ---------------------------------------------------------------------------
# README rewrite
# ---------------------------------------------------------------------------


class TestRewriteReadmeSamplesSection:
    def test_inserts_section_between_markers(self):
        original = (
            f"# Project\n\nIntro.\n\n{SAMPLES_BEGIN}\nold sample list\n{SAMPLES_END}\n\nTail."
        )
        new = rewrite_readme_samples_section(original, "new sample list")
        assert SAMPLES_BEGIN in new
        assert SAMPLES_END in new
        assert "new sample list" in new
        assert "old sample list" not in new
        assert "# Project" in new
        assert "Tail." in new

    def test_raises_if_markers_missing(self):
        import pytest

        with pytest.raises(ValueError, match="marker"):
            rewrite_readme_samples_section("README without markers", "new content")

    def test_does_not_eat_surrounding_text(self):
        original = (
            f"# Title\n\n{SAMPLES_BEGIN}\nold\n{SAMPLES_END}\n\n## After samples\n\nMore content."
        )
        new = rewrite_readme_samples_section(original, "fresh content")
        assert "## After samples" in new
        assert "More content." in new
        assert "# Title" in new
