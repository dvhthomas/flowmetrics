"""Behavioural spec for the WorkItem → StageTransition bridge.

The bridge lets Phase 1 build a canonical stream WITHOUT
rewriting source adapters. It reads the WorkItem types the
sources already emit and converts their `status_intervals` into
the canonical `StageTransition` stream.

The bridge is intentionally simple: each `StatusInterval` starts
a row. The signal is picked from the source (inferred from the
WorkItem's `item_id` prefix when given the source-agnostic
entry-point).

Once every metric reads the canonical stream natively
(Phase 4.5), this bridge is deleted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from flowmetrics import signals
from flowmetrics.compute import StatusInterval, WorkItem
from flowmetrics.sources.intervals import (
    github_workitem_to_transitions,
    jira_workitem_to_transitions,
    workitem_to_transitions,
)


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# Modeled on real cache shapes:
#   Jira: CASSANDRA-21251 had Triage Needed → Open → In Progress → Patch Available
#   GitHub: rustlang/rust PR #45660 had Open → Review → Merge Queue → Merged
JIRA_CASSANDRA_WORKITEM = WorkItem(
    item_id="jira:CASSANDRA:21251",
    title="Transient mismatching partitioner error when querying legacy",
    created_at=_ts(2026, 3, 25, 10, 8, 12),
    completed_at=None,
    status_intervals=[
        StatusInterval(_ts(2026, 3, 25, 10, 8, 12), _ts(2026, 3, 25, 10, 17, 4), "Triage Needed"),
        StatusInterval(_ts(2026, 3, 25, 10, 17, 4), _ts(2026, 3, 25, 10, 17, 30), "Open"),
        StatusInterval(_ts(2026, 3, 25, 10, 17, 30), _ts(2026, 3, 30, 16, 49, 48), "In Progress"),
        StatusInterval(_ts(2026, 3, 30, 16, 49, 48), _ts(2026, 5, 15), "Patch Available"),
    ],
    url="https://issues.apache.org/jira/browse/CASSANDRA-21251",
)


# A GitHub PR fully materialized through github_labels.materialize_status_intervals
# (the same path the live source uses): four stages visited.
GITHUB_PR_WORKITEM = WorkItem(
    item_id="github:rust-lang/rust:pr:45660",
    title="feat: foo",
    created_at=_ts(2026, 4, 27, 17, 49, 23),
    completed_at=_ts(2026, 4, 28, 16, 35, 2),
    status_intervals=[
        StatusInterval(_ts(2026, 4, 27, 17, 49, 23), _ts(2026, 4, 27, 18, 2, 36), "Open"),
        StatusInterval(_ts(2026, 4, 27, 18, 2, 36), _ts(2026, 4, 28, 2, 10, 59), "Review"),
        StatusInterval(_ts(2026, 4, 28, 2, 10, 59), _ts(2026, 4, 28, 16, 35, 2), "Merge Queue"),
        StatusInterval(_ts(2026, 4, 28, 16, 35, 2), _ts(2026, 4, 28, 16, 35, 2), "Merged"),
    ],
    url="https://github.com/rust-lang/rust/pull/45660",
)


class TestJiraBridge:
    def test_each_interval_becomes_a_transition(self):
        txs = jira_workitem_to_transitions(JIRA_CASSANDRA_WORKITEM)
        assert len(txs) == 4
        stages = [t.stage for t in txs]
        assert stages == ["Triage Needed", "Open", "In Progress", "Patch Available"]
        # entered_at matches each interval's start
        starts = [t.entered_at for t in txs]
        assert starts == [iv.start for iv in JIRA_CASSANDRA_WORKITEM.status_intervals]
        # item_id flows through
        assert {t.item_id for t in txs} == {"jira:CASSANDRA:21251"}

    def test_first_transition_carries_issue_created_signal(self):
        txs = jira_workitem_to_transitions(JIRA_CASSANDRA_WORKITEM)
        assert txs[0].signal == signals.SIGNAL_JIRA_ISSUE_CREATED

    def test_subsequent_transitions_carry_status_changed_signal(self):
        txs = jira_workitem_to_transitions(JIRA_CASSANDRA_WORKITEM)
        assert [t.signal for t in txs[1:]] == [signals.SIGNAL_JIRA_STATUS_CHANGED] * 3

    def test_resolved_signal_when_item_is_completed(self):
        resolved = WorkItem(
            item_id="jira:ENG:1",
            title="x",
            created_at=_ts(2026, 5, 1),
            completed_at=_ts(2026, 5, 5, 12),
            status_intervals=[
                StatusInterval(_ts(2026, 5, 1), _ts(2026, 5, 3), "Open"),
                StatusInterval(_ts(2026, 5, 3), _ts(2026, 5, 5, 12), "In Progress"),
                StatusInterval(_ts(2026, 5, 5, 12), _ts(2026, 5, 5, 12), "Done"),
            ],
            url="https://issues.example.com/browse/ENG-1",
        )
        txs = jira_workitem_to_transitions(resolved)
        assert txs[-1].signal == signals.SIGNAL_JIRA_RESOLVED
        assert txs[-1].stage == "Done"

    def test_empty_intervals_produce_no_transitions(self):
        empty = WorkItem(
            item_id="jira:ENG:2",
            title="x",
            created_at=_ts(2026, 5, 1),
            completed_at=None,
            status_intervals=[],
        )
        assert jira_workitem_to_transitions(empty) == []


class TestGitHubBridge:
    def test_each_interval_becomes_a_transition(self):
        txs = github_workitem_to_transitions(GITHUB_PR_WORKITEM)
        assert len(txs) == 4
        assert [t.stage for t in txs] == ["Open", "Review", "Merge Queue", "Merged"]

    def test_first_transition_carries_pr_created(self):
        txs = github_workitem_to_transitions(GITHUB_PR_WORKITEM)
        assert txs[0].signal == signals.SIGNAL_GITHUB_PR_CREATED

    def test_mid_transitions_carry_label_added(self):
        txs = github_workitem_to_transitions(GITHUB_PR_WORKITEM)
        # spec calls out: bridge collapses every middle interval to
        # github-label-added; finer-grained PR lifecycle signals are
        # added by the Phase-2-aware native adapter, not by this bridge.
        assert [t.signal for t in txs[1:-1]] == [signals.SIGNAL_GITHUB_LABEL_ADDED] * 2

    def test_terminal_transition_is_pr_merged_when_completed(self):
        txs = github_workitem_to_transitions(GITHUB_PR_WORKITEM)
        assert txs[-1].signal == signals.SIGNAL_GITHUB_PR_MERGED
        assert txs[-1].stage == "Merged"

    def test_in_flight_terminal_is_just_label_added(self):
        in_flight = WorkItem(
            item_id="github:acme/widget:pr:7",
            title="wip",
            created_at=_ts(2026, 5, 1),
            completed_at=None,
            status_intervals=[
                StatusInterval(_ts(2026, 5, 1), _ts(2026, 5, 3), "Open"),
                StatusInterval(_ts(2026, 5, 3), _ts(2026, 5, 15), "Review"),
            ],
        )
        txs = github_workitem_to_transitions(in_flight)
        assert txs[-1].signal == signals.SIGNAL_GITHUB_LABEL_ADDED


class TestSourceAgnosticDispatch:
    def test_dispatches_on_item_id_prefix_jira(self):
        txs = workitem_to_transitions(JIRA_CASSANDRA_WORKITEM)
        assert txs[0].signal == signals.SIGNAL_JIRA_ISSUE_CREATED

    def test_dispatches_on_item_id_prefix_github(self):
        txs = workitem_to_transitions(GITHUB_PR_WORKITEM)
        assert txs[0].signal == signals.SIGNAL_GITHUB_PR_CREATED

    def test_unknown_prefix_raises(self):
        unknown = WorkItem(
            item_id="mystery:42",
            title="x",
            created_at=_ts(2026, 5, 1),
            completed_at=None,
            status_intervals=[StatusInterval(_ts(2026, 5, 1), _ts(2026, 5, 2), "Open")],
        )
        with pytest.raises(ValueError, match="unknown source prefix"):
            workitem_to_transitions(unknown)
