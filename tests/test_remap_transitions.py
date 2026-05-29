"""remap_transitions — relabel adapter stages to the user's steps at
materialise time (#2 Slice B).

Each transition's `stage` is rewritten to the first step it matches (OR
within a step, first step wins across steps). Unmatched transitions go
to `_unmatched`. A contract with no steps passes through unchanged
(adapter-native stages).
"""

from __future__ import annotations

from datetime import UTC, datetime

from flowmetrics.canonical import StageTransition
from flowmetrics.contract import Step
from flowmetrics.matching import UNMATCHED_STAGE, remap_transitions


def _t(stage: str, signal: str, item: str = "github:o/r:pr:1") -> StageTransition:
    return StageTransition(
        item_id=item,
        entered_at=datetime(2026, 5, 1, tzinfo=UTC),
        stage=stage,
        signal=signal,
    )


def test_remaps_stage_to_the_matching_step_name():
    steps = [
        Step(name="In Review", wip=True, matches=[{"event": "pr-ready"}]),
        Step(name="Done", wip=False, matches=[{"event": "pr-merged"}]),
    ]
    txs = [
        _t("Awaiting Review", "github-pr-ready-for-review"),
        _t("Merged", "github-pr-merged"),
    ]
    out = remap_transitions(txs, steps, source="github")
    assert [t.stage for t in out] == ["In Review", "Done"]
    # Everything else on the transition is preserved.
    assert out[0].item_id == txs[0].item_id
    assert out[0].signal == txs[0].signal


def test_unmatched_transition_goes_to_unmatched_stage():
    steps = [Step(name="Done", wip=False, matches=[{"event": "pr-merged"}])]
    out = remap_transitions([_t("Draft", "github-pr-created")], steps, source="github")
    assert out[0].stage == UNMATCHED_STAGE


def test_no_steps_passes_through_unchanged():
    txs = [_t("Draft", "github-pr-created")]
    assert remap_transitions(txs, [], source="github") == txs


def test_first_matching_step_wins():
    steps = [
        Step(name="A", matches=[{"label": "x"}]),
        Step(name="B", matches=[{"label": "x"}]),
    ]
    out = remap_transitions([_t("x", "github-label-added")], steps, source="github")
    assert out[0].stage == "A"


def test_step_without_matchers_matches_its_own_name_as_a_stage():
    steps = [Step(name="Merged", wip=False)]  # no matchers → stage:Merged
    out = remap_transitions([_t("Merged", "github-pr-merged")], steps, source="github")
    assert out[0].stage == "Merged"
