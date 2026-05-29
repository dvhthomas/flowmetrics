"""The single step-matcher evaluator.

Both the dry-run preview (`source_probe.bucket_items_by_step`) and the
materialise remap (`materialise.remap_transitions`) decide "does this
transition belong to this step?" through `matcher_matches` here, so the
preview can never diverge from what materialise actually writes.

A transition has two axes:
  - `signal` — the named lifecycle event (`github-pr-merged`, …);
  - `stage`  — the label / status / raw stage text.

An `event` matcher targets `signal` (via the source's code→signal map);
`label` / `status` / `stage` matchers target `stage`.
"""

from __future__ import annotations

from . import signals
from .contract import Matcher, Step


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
