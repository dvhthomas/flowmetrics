"""C1 — Pydantic Contract + Step + new `steps:` YAML shape.

Tests the new canonical contract model:

  - Each workflow stage is a `Step(name, wip)` row in an ordered
    `steps` list.
  - The YAML shape becomes `steps: [{name, wip}, ...]`.
  - The legacy `states: {backlog, wip, done}` YAML shape is still
    accepted on import; it's normalised to `steps` internally and
    re-emitted in the new shape.
  - A `Contract.states` compatibility shim synthesises the old
    `WorkflowStates(backlog, wip, done)` object so every CFD /
    Aging / charts caller keeps working without modification.

The full existing test suite is the regression check for the shim —
it must stay green when this file is added and the model rewritten.
"""

from __future__ import annotations

from datetime import date

import pytest


class TestStepModel:
    def test_step_carries_name_and_wip_flag(self):
        from flowmetrics.workflow import Step
        s = Step(name="In Progress", wip=True)
        assert s.name == "In Progress"
        assert s.wip is True

    def test_step_defaults_wip_to_false(self):
        from flowmetrics.workflow import Step
        s = Step(name="Open")
        assert s.wip is False

    def test_step_rejects_empty_name(self):
        from pydantic import ValidationError

        from flowmetrics.workflow import Step
        with pytest.raises(ValidationError):
            Step(name="", wip=True)

    def test_step_carries_match_identifiers(self):
        """A step is a logical bucket; `matches` lists the typed
        conditions (labels, statuses, lifecycle events) whose
        materialized data lands in this step."""
        from flowmetrics.workflow import Matcher, Step
        s = Step(
            name="Ready",
            wip=False,
            matches=[{"status": "ready"}, {"label": "ready"}],
        )
        assert s.matches == [
            Matcher(kind="status", value="ready"),
            Matcher(kind="label", value="ready"),
        ]

    def test_step_matches_default_to_empty_list(self):
        """Empty `matches` triggers the legacy lookup: the step's
        `name` itself is treated as the identifier. Existing demo
        YAMLs whose `states:` block names are the source-native
        stage names keep working unchanged."""
        from flowmetrics.workflow import Step
        s = Step(name="Draft")
        assert s.matches == []

    def test_effective_matchers_falls_back_to_name(self):
        """`Step.effective_matchers` is the lookup helper: empty
        list → a single `stage` matcher on the step's name. Use this
        wherever the query layer needs "what does this step capture"."""
        from flowmetrics.workflow import Matcher, Step
        bare = Step(name="Draft")
        with_matches = Step(name="Ready", matches=[{"status": "ready"}])
        assert bare.effective_matchers == (Matcher(kind="stage", value="Draft"),)
        assert with_matches.effective_matchers == (
            Matcher(kind="status", value="ready"),
        )


class TestContractModel:
    def test_minimal_github_contract(self):
        from flowmetrics.workflow import Contract
        c = Contract(name="foo", source="github", repo="owner/repo")
        assert c.name == "foo"
        assert c.source == "github"
        assert c.repo == "owner/repo"
        assert c.steps == []
        assert c.label is None

    def test_minimal_jira_contract(self):
        from flowmetrics.workflow import Contract
        c = Contract(
            name="bar", source="jira",
            jira_url="https://j.example.com",
            jira_project="X",
        )
        assert c.source == "jira"

    def test_steps_preserve_insertion_order(self):
        from flowmetrics.workflow import Contract, Step
        c = Contract(
            name="x", source="github", repo="a/b",
            steps=[
                Step(name="Triage", wip=False),
                Step(name="In Progress", wip=True),
                Step(name="Review", wip=True),
                Step(name="Done", wip=False),
            ],
        )
        assert [s.name for s in c.steps] == [
            "Triage", "In Progress", "Review", "Done",
        ]


class TestStatesCompatibilityShim:
    """`Contract.states` must look like the old WorkflowStates so
    every existing caller (CFD, Aging, charts/cfd, app.py) keeps
    working. Synthesis rule:
      - leading non-WIP steps → backlog
      - contiguous WIP block → wip
      - trailing non-WIP steps → done
    """

    def _c(self, *steps):
        from flowmetrics.workflow import Contract, Step
        return Contract(
            name="x", source="github", repo="a/b",
            steps=[Step(name=n, wip=w) for n, w in steps],
        )

    def test_no_steps_has_no_states(self):
        c = self._c()
        # Mirror the old behaviour: no `states:` block → None.
        assert c.states is None

    def test_canonical_workflow_synthesises_backlog_wip_done(self):
        c = self._c(
            ("Triage", False),
            ("Open", False),
            ("In Progress", True),
            ("Review", True),
            ("Approved", True),
            ("Merged", False),
            ("Closed", False),
        )
        assert c.states.backlog == ("Triage", "Open")
        assert c.states.wip == ("In Progress", "Review", "Approved")
        assert c.states.done == ("Merged", "Closed")

    def test_no_backlog_when_first_step_is_wip(self):
        c = self._c(
            ("In Progress", True),
            ("Review", True),
            ("Done", False),
        )
        assert c.states.backlog == ()
        assert c.states.wip == ("In Progress", "Review")
        assert c.states.done == ("Done",)

    def test_no_done_when_last_step_is_wip(self):
        c = self._c(
            ("Triage", False),
            ("In Progress", True),
        )
        assert c.states.backlog == ("Triage",)
        assert c.states.wip == ("In Progress",)
        assert c.states.done == ()

    def test_all_wip_yields_only_wip(self):
        c = self._c(("A", True), ("B", True))
        assert c.states.backlog == ()
        assert c.states.wip == ("A", "B")
        assert c.states.done == ()


class TestParseNewShape:
    """`parse_workflow_text` reads the new `steps:` YAML shape."""

    def test_minimal_steps_yaml_roundtrips(self):
        from flowmetrics.workflow import parse_workflow_text
        c = parse_workflow_text(
            "contract:\n"
            "  name: x\n"
            "  source: github\n"
            "  repo: a/b\n"
            "  steps:\n"
            "    - name: Draft\n"
            "      wip: false\n"
            "    - name: In Progress\n"
            "      wip: true\n"
            "    - name: Merged\n"
            "      wip: false\n",
            "x",
        )
        assert [s.name for s in c.steps] == ["Draft", "In Progress", "Merged"]
        assert [s.wip for s in c.steps] == [False, True, False]

    def test_steps_with_matches_round_trip(self):
        """The typed `matches:` per-step list survives parse + emit."""
        from flowmetrics.workflow import (
            Matcher,
            emit_canonical_yaml,
            parse_workflow_text,
        )
        c = parse_workflow_text(
            "contract:\n"
            "  name: x\n"
            "  source: github\n"
            "  repo: a/b\n"
            "  steps:\n"
            "    - name: Ready\n"
            "      wip: false\n"
            "      matches:\n"
            "        - label: ready\n"
            "        - label: triage\n"
            "    - name: Review\n"
            "      wip: true\n"
            "      matches:\n"
            "        - event: pr-ready\n",
            "x",
        )
        assert c.steps[0].matches == [
            Matcher(kind="label", value="ready"),
            Matcher(kind="label", value="triage"),
        ]
        assert c.steps[1].matches == [Matcher(kind="event", value="pr-ready")]
        # And re-emitting + re-parsing is identity.
        again = parse_workflow_text(emit_canonical_yaml(c), "x")
        assert again == c

    def test_steps_wip_defaults_to_false(self):
        from flowmetrics.workflow import parse_workflow_text
        c = parse_workflow_text(
            "contract:\n"
            "  name: x\n"
            "  source: github\n"
            "  repo: a/b\n"
            "  steps:\n"
            "    - name: Just A Name\n",  # no wip key
            "x",
        )
        assert c.steps[0].wip is False


class TestParseLegacyShape:
    """Old `states: {backlog, wip, done}` YAMLs are still accepted —
    the migration would convert them, and existing test fixtures
    (and the demo YAMLs in samples/) all use this shape today."""

    def test_old_shape_yaml_imports_as_ordered_steps(self):
        from flowmetrics.workflow import parse_workflow_text
        c = parse_workflow_text(
            "contract:\n"
            "  name: x\n"
            "  source: github\n"
            "  repo: a/b\n"
            "  states:\n"
            "    backlog: [Triage]\n"
            "    wip: [In Progress, Review]\n"
            "    done: [Merged]\n",
            "x",
        )
        # backlog → leading non-WIP; wip → wip:true; done → trailing non-WIP
        assert [(s.name, s.wip) for s in c.steps] == [
            ("Triage", False),
            ("In Progress", True),
            ("Review", True),
            ("Merged", False),
        ]
        # The shim still works on a contract created from the old shape.
        assert c.states.backlog == ("Triage",)
        assert c.states.wip == ("In Progress", "Review")
        assert c.states.done == ("Merged",)


class TestEmitCanonical:
    def test_emit_writes_steps_shape(self):
        from flowmetrics.workflow import (
            Contract,
            Step,
            emit_canonical_yaml,
            parse_workflow_text,
        )
        c = Contract(
            name="x", source="github", repo="a/b",
            label="Demo",
            start=date(2026, 5, 1), stop=date(2026, 5, 31),
            steps=[
                Step(name="Draft", wip=False),
                Step(name="In Progress", wip=True),
                Step(name="Merged", wip=False),
            ],
        )
        text = emit_canonical_yaml(c)
        # New canonical shape — no `states:` block.
        assert "steps:" in text
        assert "states:" not in text
        # Round-trip.
        parsed = parse_workflow_text(text, "x")
        assert parsed == c

    def test_parse_then_emit_then_parse_is_idempotent(self):
        from flowmetrics.workflow import (
            emit_canonical_yaml,
            parse_workflow_text,
        )
        original_yaml = (
            "contract:\n"
            "  name: x\n"
            "  source: github\n"
            "  repo: a/b\n"
            "  states:\n"
            "    backlog: [Open]\n"
            "    wip: [In Progress]\n"
            "    done: [Closed]\n"
        )
        once = parse_workflow_text(original_yaml, "x")
        twice = parse_workflow_text(emit_canonical_yaml(once), "x")
        assert once == twice


class TestLegacyWorkflowStatesStillImportable:
    """A lot of existing tests construct WorkflowStates directly.
    The name has to stay importable from flowmetrics.contract."""

    def test_workflowstates_name_is_importable(self):
        from flowmetrics.workflow import WorkflowStates
        s = WorkflowStates(wip=("Review",), done=("Merged",))
        assert s.wip == ("Review",)
        assert s.done == ("Merged",)
