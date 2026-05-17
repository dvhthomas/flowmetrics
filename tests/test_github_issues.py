"""Behavioural spec for the GitHub Issue → canonical-stream parser.

Each Issue's `closedByPullRequestsReferences` carries the PR that
closed it (or empty when manually closed). The parser maps each
Issue to:
  - one StreamItem
  - a list of StageTransition rows reflecting label timeline +
    a terminal Done transition stitched from the closing PR's
    mergedAt (carrying SIGNAL_GITHUB_PR_CLOSES_ISSUE).

Driven by a real fixture
(tests/fixtures/canonical/github_issues_calcmark.json), distilled
from a live API call against CalcMark/go-calcmark.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flowmetrics import signals
from flowmetrics.canonical import StageTransition
from flowmetrics.sources.github_issues import parse_issue_node, parse_search_result
from flowmetrics.stream import StreamItem

FIXTURE = Path(__file__).parent / "fixtures" / "canonical" / "github_issues_calcmark.json"


@pytest.fixture
def issue_nodes() -> list[dict]:
    raw = json.loads(FIXTURE.read_text())
    return raw["data"]["search"]["nodes"]


class TestParseIssueNode:
    def test_returns_streamitem_with_canonical_id_and_url(self, issue_nodes):
        node = next(n for n in issue_nodes if n["number"] == 131)
        item, _ = parse_issue_node(node, repo="CalcMark/go-calcmark")
        assert isinstance(item, StreamItem)
        assert item.item_id == "github:CalcMark/go-calcmark:issue:131"
        assert item.url == node["url"]
        assert item.title == node["title"]
        assert item.completed_at is not None

    def test_completed_at_uses_pr_merged_when_stitched(self, issue_nodes):
        """The cycle-time-relevant 'done' is the closing PR's mergedAt,
        not the Issue's own closedAt (which is the side-effect)."""
        node = next(n for n in issue_nodes if n["number"] == 131)
        item, _ = parse_issue_node(node, repo="CalcMark/go-calcmark")
        merged = node["closedByPullRequestsReferences"]["nodes"][0]["mergedAt"]
        assert item.completed_at.isoformat().startswith(merged[:19])

    def test_stitched_terminal_signal_is_pr_closes_issue(self, issue_nodes):
        node = next(n for n in issue_nodes if n["number"] == 131)
        _, txs = parse_issue_node(node, repo="CalcMark/go-calcmark")
        assert txs[-1].signal == signals.SIGNAL_GITHUB_PR_CLOSES_ISSUE
        # terminal entered_at should equal the closing PR's mergedAt
        closing = node["closedByPullRequestsReferences"]["nodes"][0]
        assert txs[-1].entered_at.isoformat().startswith(closing["mergedAt"][:19])

    def test_naked_issue_terminal_signal_is_issue_closed(self, issue_nodes):
        # Issue #160 was closed but has no linked closing PR.
        node = next(n for n in issue_nodes if n["number"] == 160)
        _, txs = parse_issue_node(node, repo="CalcMark/go-calcmark")
        assert txs[-1].signal == signals.SIGNAL_GITHUB_ISSUE_CLOSED

    def test_first_transition_is_issue_created(self, issue_nodes):
        node = next(n for n in issue_nodes if n["number"] == 131)
        _, txs = parse_issue_node(node, repo="CalcMark/go-calcmark")
        assert txs[0].signal == signals.SIGNAL_GITHUB_ISSUE_CREATED
        assert txs[0].entered_at.isoformat().startswith(node["createdAt"][:19])

    def test_label_events_become_transitions(self, issue_nodes):
        # Issue #133 has 2 label timeline items.
        node = next(n for n in issue_nodes if n["number"] == 133)
        _, txs = parse_issue_node(node, repo="CalcMark/go-calcmark")
        # At least one mid-stream transition carrying label-added/removed.
        mid_signals = {t.signal for t in txs[1:-1]}
        assert mid_signals & {
            signals.SIGNAL_GITHUB_LABEL_ADDED,
            signals.SIGNAL_GITHUB_LABEL_REMOVED,
        }

    def test_transitions_are_sorted_chronologically(self, issue_nodes):
        node = next(n for n in issue_nodes if n["number"] == 131)
        _, txs = parse_issue_node(node, repo="CalcMark/go-calcmark")
        starts = [t.entered_at for t in txs]
        assert starts == sorted(starts)

    def test_each_transition_carries_global_item_id(self, issue_nodes):
        node = next(n for n in issue_nodes if n["number"] == 131)
        _, txs = parse_issue_node(node, repo="CalcMark/go-calcmark")
        assert all(t.item_id == "github:CalcMark/go-calcmark:issue:131" for t in txs)


class TestParseSearchResult:
    def test_returns_one_entry_per_issue_node(self, issue_nodes):
        raw = json.loads(FIXTURE.read_text())
        entries = parse_search_result(raw, repo="CalcMark/go-calcmark")
        assert len(entries) == len(issue_nodes)

    def test_returns_pairs_of_streamitem_and_transitions(self, issue_nodes):
        raw = json.loads(FIXTURE.read_text())
        entries = parse_search_result(raw, repo="CalcMark/go-calcmark")
        for item, txs in entries:
            assert isinstance(item, StreamItem)
            assert all(isinstance(t, StageTransition) for t in txs)
            assert len(txs) >= 2  # created + closed minimum
