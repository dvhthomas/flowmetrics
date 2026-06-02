"""The single step-matcher evaluator.

Both the dry-run preview (`source_probe.bucket_items_by_step`) and the
materialize remap (`materialize.remap_transitions`) decide "does this
transition belong to this step?" through `matcher_matches` here, so the
preview can never diverge from what materialize actually writes.

A transition has two axes:
  - `signal` — the named lifecycle event (`github-pr-merged`, …);
  - `stage`  — the label / status / raw stage text.

An `event` matcher targets `signal` (via the source's code→signal map);
`label` / `status` / `stage` matchers target `stage`.
"""

from __future__ import annotations

from dataclasses import replace

from . import signals
from .canonical import StageTransition
from .workflow import Matcher, Step

# Stage assigned to a transition that matches no step — surfaced as a
# coverage-gap bucket rather than silently dropped.
UNMATCHED_STAGE = "_unmatched"


def matcher_matches(
    matcher: Matcher, *, source: str, stage: str, signal: str | None,
) -> bool:
    """True when a transition `(stage, signal)` satisfies `matcher`."""
    if matcher.kind == "event":
        target = signals.event_codes_for(source).get(matcher.value)
        return signal is not None and signal == target
    # label / status / stage all compare the stage text.
    return stage == matcher.value


def step_for(
    step: Step, *, source: str, stage: str, signal: str | None,
) -> bool:
    """True when a transition belongs to `step` (any of its effective
    matchers matches — OR semantics)."""
    return any(
        matcher_matches(m, source=source, stage=stage, signal=signal)
        for m in step.effective_matchers
    )


def remap_transitions(
    transitions: list[StageTransition],
    steps: list[Step],
    *,
    source: str,
) -> list[StageTransition]:
    """Relabel each transition's `stage` to the step it belongs to.

    First step wins (steps are in kanban order); a transition matching
    no step is relabelled `_unmatched`. With no steps, transitions pass
    through unchanged (adapter-native stages — backward compatible).
    """
    if not steps:
        return list(transitions)
    out: list[StageTransition] = []
    for t in transitions:
        stage = UNMATCHED_STAGE
        for step in steps:
            if step_for(step, source=source, stage=t.stage, signal=t.signal):
                stage = step.name
                break
        out.append(replace(t, stage=stage))
    return out
