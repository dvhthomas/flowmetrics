"""Typed step matchers + the shared evaluator (#2 Slice A).

A step's `matches` is a list of typed mappings — `{event|label|status|
stage: value}` — not bare strings. `event` values are short codes from
the source's vocabulary (signals.event_codes_for). One evaluator,
`matching.matcher_matches`, is shared by the dry-run preview and the
materialize remap so they can never diverge.
"""

from __future__ import annotations

import pytest

from flowmetrics.workflow import (
    WorkflowError,
    Matcher,
    emit_canonical_yaml,
    parse_workflow_text,
)
from flowmetrics.matching import matcher_matches


def _parse(steps_yaml: str, source: str = "github", target: str = "  repo: o/r\n"):
    return parse_workflow_text(
        f"contract:\n  name: c\n  source: {source}\n{target}{steps_yaml}", "c"
    )


class TestParseTypedMatchers:
    def test_parses_event_and_label_matchers(self):
        c = _parse(
            "  steps:\n"
            "    - name: In Review\n"
            "      wip: true\n"
            "      matches:\n"
            "        - event: pr-ready\n"
            "        - label: needs-review\n"
        )
        step = c.steps[0]
        assert [(m.kind, m.value) for m in step.matches] == [
            ("event", "pr-ready"),
            ("label", "needs-review"),
        ]

    def test_bare_string_matcher_is_rejected(self):
        with pytest.raises(WorkflowError):
            _parse(
                "  steps:\n"
                "    - name: X\n"
                "      matches:\n"
                "        - ready\n"
            )

    def test_unknown_event_code_is_rejected(self):
        with pytest.raises(WorkflowError):
            _parse(
                "  steps:\n"
                "    - name: X\n"
                "      matches:\n"
                "        - event: not-a-real-code\n"
            )

    def test_jira_event_code_rejected_on_github_source(self):
        with pytest.raises(WorkflowError):
            _parse(
                "  steps:\n"
                "    - name: X\n"
                "      matches:\n"
                "        - event: status-changed\n"  # a Jira code
            )

    def test_jira_event_code_accepted_on_jira_source(self):
        c = _parse(
            "  steps:\n"
            "    - name: Doing\n"
            "      matches:\n"
            "        - event: status-changed\n",
            source="jira",
            target="  jira_url: https://j\n  jira_project: P\n",
        )
        assert c.steps[0].matches[0].kind == "event"


class TestEffectiveMatchers:
    def test_empty_matches_falls_back_to_stage_named_for_the_step(self):
        c = _parse("  steps:\n    - name: Done\n      wip: false\n")
        assert c.steps[0].effective_matchers == (
            Matcher(kind="stage", value="Done"),
        )


class TestEmitRoundTrip:
    def test_typed_matchers_round_trip_through_yaml(self):
        c = _parse(
            "  steps:\n"
            "    - name: Merged\n"
            "      wip: false\n"
            "      matches:\n"
            "        - event: pr-merged\n"
        )
        again = parse_workflow_text(emit_canonical_yaml(c), "c")
        m = again.steps[0].matches[0]
        assert (m.kind, m.value) == ("event", "pr-merged")


class TestEvaluator:
    def test_event_matcher_matches_signal_not_stage(self):
        m = Matcher(kind="event", value="pr-merged")
        assert matcher_matches(
            m, source="github", stage="Merged", signal="github-pr-merged"
        )
        assert not matcher_matches(
            m, source="github", stage="Merged", signal="github-pr-created"
        )

    def test_label_matcher_matches_stage_text(self):
        m = Matcher(kind="label", value="needs-review")
        assert matcher_matches(
            m, source="github", stage="needs-review", signal="github-label-added"
        )
        assert not matcher_matches(
            m, source="github", stage="other", signal=None
        )

    def test_stage_matcher_matches_raw_stage(self):
        m = Matcher(kind="stage", value="Awaiting Review")
        assert matcher_matches(
            m, source="github", stage="Awaiting Review", signal=None
        )
