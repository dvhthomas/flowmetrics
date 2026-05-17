"""Behavioural spec for the canonical Stream.

A `Stream` is the runtime collection of (item, transitions) that
the metric layer reads from. Conceptually it is the two-table
model — `work_items` + `stage_transitions` — held in memory.

The metric layer never asks "what source did this come from?"
It asks Stream questions: "what items were in WIP on this date,"
"what's the current stage of item X," "transitions in window
[a, b)." Every flow metric is one of those.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from flowmetrics.canonical import StageTransition, WorkflowDef
from flowmetrics.stream import Stream, StreamItem, load_stream_from_json

FIXTURES = Path(__file__).parent / "fixtures" / "canonical"


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestStreamConstruction:
    def test_construction_from_items_and_transitions(self):
        wf = WorkflowDef(stages=("Open", "Done"), wip_set=frozenset({"Open"}))
        items = [StreamItem(item_id="x:1", title="t", url=None,
                            created_at=_ts(2026, 5, 1), completed_at=None)]
        txs = [StageTransition("x:1", _ts(2026, 5, 1), "Open", "any-signal")]
        s = Stream(items=items, transitions=txs, workflow=wf)
        assert len(list(s)) == 1
        assert s.workflow is wf

    def test_validates_transitions_reference_known_items(self):
        wf = WorkflowDef(stages=("Open",), wip_set=frozenset({"Open"}))
        items = [StreamItem("x:1", "t", None, _ts(2026, 5, 1), None)]
        bogus = [StageTransition("x:2", _ts(2026, 5, 1), "Open", "s")]
        with pytest.raises(ValueError, match="unknown item_id"):
            Stream(items=items, transitions=bogus, workflow=wf)

    def test_validates_transitions_reference_known_stages(self):
        wf = WorkflowDef(stages=("Open",), wip_set=frozenset({"Open"}))
        items = [StreamItem("x:1", "t", None, _ts(2026, 5, 1), None)]
        bad = [StageTransition("x:1", _ts(2026, 5, 1), "Triaged", "s")]
        with pytest.raises(ValueError, match="unknown stage"):
            Stream(items=items, transitions=bad, workflow=wf)


class TestStreamQueries:
    def _stream(self) -> Stream:
        wf = WorkflowDef(
            stages=("Open", "Triaged", "Done"),
            wip_set=frozenset({"Triaged"}),
        )
        items = [
            StreamItem("x:1", "a", None, _ts(2026, 5, 1), _ts(2026, 5, 10)),
            StreamItem("x:2", "b", None, _ts(2026, 5, 3), None),
        ]
        txs = [
            StageTransition("x:1", _ts(2026, 5, 1), "Open", "s"),
            StageTransition("x:1", _ts(2026, 5, 5), "Triaged", "s"),
            StageTransition("x:1", _ts(2026, 5, 10), "Done", "s"),
            StageTransition("x:2", _ts(2026, 5, 3), "Open", "s"),
            StageTransition("x:2", _ts(2026, 5, 7), "Triaged", "s"),
        ]
        return Stream(items=items, transitions=txs, workflow=wf)

    def test_current_stage_at_date_returns_most_recent_transition_stage(self):
        s = self._stream()
        # x:1 had Open→Triaged→Done; on May 6 it's Triaged.
        assert s.current_stage_at("x:1", date(2026, 5, 6)) == "Triaged"
        assert s.current_stage_at("x:1", date(2026, 5, 11)) == "Done"

    def test_current_stage_before_creation_is_none(self):
        s = self._stream()
        assert s.current_stage_at("x:1", date(2026, 4, 30)) is None

    def test_in_flight_items_at_asof_excludes_completed(self):
        s = self._stream()
        # On May 8, x:1 is Triaged (in WIP), x:2 is Triaged (in WIP).
        ids = {i.item_id for i in s.in_flight_at(date(2026, 5, 8))}
        assert ids == {"x:1", "x:2"}
        # On May 12, x:1 is Done — not in WIP — dropped.
        ids = {i.item_id for i in s.in_flight_at(date(2026, 5, 12))}
        assert ids == {"x:2"}

    def test_transitions_for_returns_only_that_item_in_chronological_order(self):
        s = self._stream()
        txs = list(s.transitions_for("x:1"))
        assert [t.stage for t in txs] == ["Open", "Triaged", "Done"]
        assert all(
            txs[i].entered_at <= txs[i + 1].entered_at
            for i in range(len(txs) - 1)
        )


class TestJsonLoader:
    def test_loads_issue_pr_stitched_fixture(self):
        s = load_stream_from_json(FIXTURES / "github_issue_pr_stitched.json")
        ids = {i.item_id for i in s}
        assert ids == {
            "github:acme/widget:issue:101",
            "github:acme/widget:pr:215",
        }
        # Issue closed by PR-merge — terminal transition on the Issue
        # stream carries the cross-source linking signal.
        issue_txs = list(s.transitions_for("github:acme/widget:issue:101"))
        assert issue_txs[-1].signal == "github-pr-closes-issue"
        assert issue_txs[-1].stage == "Done"

    def test_loads_mixed_team_fixture_with_three_workflows(self):
        # The mixed fixture has multiple workflows; a per-source
        # WorkflowDef applies at the *item* level. The simple
        # single-workflow loader rejects this — verifying we don't
        # silently flatten heterogeneous data.
        with pytest.raises(ValueError, match="workflows"):
            load_stream_from_json(FIXTURES / "mixed_team_stream.json")
