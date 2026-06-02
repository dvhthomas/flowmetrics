"""Tests for the contract YAML's `states:` block — the 3-category
classification of workflow states.

Schema:

    contract:
      ...
      states:
        backlog: [Triage Needed, Open]        # excluded from CFD
        wip:     [In Progress, Patch Avail.]  # CFD bands + Aging filter
        done:    [Resolved]                   # CFD departures; not Aging

Categories carry an explicit kanban order WITHIN each list. The
list-of-lists is ordered backlog → wip → done semantically (and
that's how charts compose bands), but the YAML lists themselves
are flat — there's no "renaming" or aggregation.

Order rule: whatever the YAML gives is the order. No inference.
Reclassify by moving a state name between lists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from flowmetrics.workflow import WorkflowError, load_contract


def _write(tmp: Path, name: str, body: dict) -> Path:
    p = tmp / f"{name}.yaml"
    p.write_text(
        yaml.safe_dump(
            {"contract": {"name": name, **body}}, sort_keys=False
        )
    )
    return tmp


class TestLabelField:
    """Optional `label:` — human-friendly display name. The
    canonical `name` field stays the routing ID (must be unique
    + URL-safe); `label` is the prose the UI shows in
    breadcrumbs and the home-page workflow list. Falls back
    to `name` when omitted."""

    def test_label_optional(self, tmp_path):
        _write(tmp_path, "demo", {
            "source": "github",
            "repo": "owner/name",
            "start": "2026-05-04",
            "stop": "2026-05-10",
        })
        c = load_contract("demo", tmp_path)
        assert c.label is None, (
            "label defaults to None when omitted (callers fall "
            "back to name for display)"
        )

    def test_label_parses_as_string(self, tmp_path):
        _write(tmp_path, "apache-cassandra-week", {
            "label": "Cassandra",
            "source": "jira",
            "jira_url": "https://issues.apache.org/jira",
            "jira_project": "CASSANDRA",
            "start": "2025-04-01",
            "stop": "2025-04-07",
        })
        c = load_contract("apache-cassandra-week", tmp_path)
        assert c.label == "Cassandra"

    def test_label_non_string_raises(self, tmp_path):
        _write(tmp_path, "demo", {
            "label": 42,
            "source": "github",
            "repo": "owner/name",
            "start": "2026-05-04",
            "stop": "2026-05-10",
        })
        with pytest.raises(WorkflowError, match=r"label.*string"):
            load_contract("demo", tmp_path)


class TestStatesBlockParsing:
    def test_contract_without_states_block_has_none(self, tmp_path):
        _write(tmp_path, "demo", {
            "source": "github",
            "repo": "owner/name",
            "start": "2026-05-04",
            "stop": "2026-05-10",
        })
        c = load_contract("demo", tmp_path)
        assert c.states is None

    def test_states_block_parses_all_three_categories(self, tmp_path):
        _write(tmp_path, "demo", {
            "source": "jira",
            "jira_url": "https://issues.apache.org/jira",
            "jira_project": "CASSANDRA",
            "start": "2025-04-01",
            "stop": "2025-04-07",
            "states": {
                "backlog": ["Triage Needed", "Triage", "Open"],
                "wip": [
                    "In Progress", "Reopened", "Patch Available",
                    "Review In Progress", "Needs Reviewer",
                ],
                "done": ["Resolved"],
            },
        })
        c = load_contract("demo", tmp_path)
        assert c.states is not None
        assert c.states.backlog == ("Triage Needed", "Triage", "Open")
        assert c.states.wip[:2] == ("In Progress", "Reopened")
        assert c.states.done == ("Resolved",)

    def test_yaml_order_preserved_for_kanban(self, tmp_path):
        """Order is part of the schema — list order = kanban
        left-to-right (top of CFD stack = first wip)."""
        _write(tmp_path, "demo", {
            "source": "github",
            "repo": "owner/name",
            "start": "2026-05-04",
            "stop": "2026-05-10",
            "states": {
                "wip": ["Draft", "Awaiting Review", "Approved"],
                "done": ["Merged"],
            },
        })
        c = load_contract("demo", tmp_path)
        assert c.states.wip == ("Draft", "Awaiting Review", "Approved"), (
            f"YAML order MUST be preserved verbatim; got {c.states.wip}"
        )

    def test_categories_may_be_empty_or_omitted(self, tmp_path):
        """Backlog may not exist for some workflows (GitHub PRs —
        a PR existing means work has begun). Each category
        defaults to empty when omitted."""
        _write(tmp_path, "demo", {
            "source": "github",
            "repo": "owner/name",
            "start": "2026-05-04",
            "stop": "2026-05-10",
            "states": {
                "wip": ["Draft", "Awaiting Review"],
                "done": ["Merged"],
            },
        })
        c = load_contract("demo", tmp_path)
        assert c.states.backlog == ()
        assert c.states.wip == ("Draft", "Awaiting Review")
        assert c.states.done == ("Merged",)

    def test_state_in_two_categories_raises(self, tmp_path):
        """A state can appear in at most one category — otherwise
        chart math becomes ambiguous (is it WIP or done?)."""
        _write(tmp_path, "demo", {
            "source": "github",
            "repo": "owner/name",
            "start": "2026-05-04",
            "stop": "2026-05-10",
            "states": {
                "wip": ["Approved"],
                "done": ["Approved"],  # duplicate
            },
        })
        with pytest.raises(WorkflowError, match=r"appears in more than one"):
            load_contract("demo", tmp_path)

    def test_unknown_category_key_raises(self, tmp_path):
        _write(tmp_path, "demo", {
            "source": "github",
            "repo": "owner/name",
            "start": "2026-05-04",
            "stop": "2026-05-10",
            "states": {
                "wip": ["In Progress"],
                "in_review": ["Patch Available"],  # not a valid category
            },
        })
        with pytest.raises(WorkflowError, match=r"unknown category"):
            load_contract("demo", tmp_path)

    def test_non_list_category_value_raises(self, tmp_path):
        _write(tmp_path, "demo", {
            "source": "github",
            "repo": "owner/name",
            "start": "2026-05-04",
            "stop": "2026-05-10",
            "states": {"wip": "Approved"},  # must be a list
        })
        with pytest.raises(WorkflowError, match=r"must be a list"):
            load_contract("demo", tmp_path)
