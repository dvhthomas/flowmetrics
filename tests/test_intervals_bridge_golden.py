"""Golden test: bridge over a real cached Jira issue.

Grounds the canonical model in authentic source data shape. The
fixture under `tests/fixtures/canonical/jira_cassandra_21301.json`
is a verbatim slice of `.cache/jira/*` for one issue with five
status changes — exactly the kind of multi-stage workflow Vacanti
talks about.

This test asserts the *contract* between the existing Jira
adapter (untouched) and the bridge: every status interval surfaces
as a StageTransition, in order, with the right signal mapping. If
the adapter changes shape or the bridge stops carrying authentic
events, this test breaks loudly.
"""

from __future__ import annotations

import json
from pathlib import Path

from flowmetrics import signals
from flowmetrics.sources.intervals import jira_workitem_to_transitions
from flowmetrics.sources.jira import _issue_to_work_item

FIXTURE = Path(__file__).parent / "fixtures" / "canonical" / "jira_cassandra_21301.json"


def _load_workitem():
    raw = json.loads(FIXTURE.read_text())
    issue = raw["issues"][0]
    return _issue_to_work_item(
        issue,
        in_flight_asof=None,
        base_url="https://issues.apache.org/jira",
    )


class TestRealJiraGolden:
    def test_workitem_matches_expected_intervals(self):
        wi = _load_workitem()
        # Pinned shape — if Jira adapter changes interval-building, we
        # want a loud failure here so we can re-examine the bridge.
        stages = [iv.status for iv in wi.status_intervals]
        assert stages == [
            "Triage Needed",
            "Open",
            "Patch Available",
            "Review In Progress",
            "Ready to Commit",
        ]
        assert wi.completed_at is not None  # resolved
        assert wi.url == "https://issues.apache.org/jira/browse/CASSANDRA-21301"

    def test_bridge_emits_one_transition_per_interval(self):
        wi = _load_workitem()
        txs = jira_workitem_to_transitions(wi)
        assert len(txs) == len(wi.status_intervals)

    def test_bridge_preserves_interval_order_and_timestamps(self):
        wi = _load_workitem()
        txs = jira_workitem_to_transitions(wi)
        # entered_at == interval.start, in declared order
        assert [t.entered_at for t in txs] == [iv.start for iv in wi.status_intervals]
        assert [t.stage for t in txs] == [iv.status for iv in wi.status_intervals]

    def test_bridge_signal_mapping_on_real_resolved_issue(self):
        wi = _load_workitem()
        txs = jira_workitem_to_transitions(wi)
        # first → created, last → resolved (because completed_at is set),
        # middle → status changed
        assert txs[0].signal == signals.SIGNAL_JIRA_ISSUE_CREATED
        assert txs[-1].signal == signals.SIGNAL_JIRA_RESOLVED
        assert all(
            t.signal == signals.SIGNAL_JIRA_STATUS_CHANGED for t in txs[1:-1]
        )

    def test_every_transition_carries_global_item_id(self):
        wi = _load_workitem()
        txs = jira_workitem_to_transitions(wi)
        assert {t.item_id for t in txs} == {"CASSANDRA-21301"}
