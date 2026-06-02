"""The canonical Stream — the two-table model held in memory.

A Stream is what every metric in this codebase reads from. It is
NOT a source: it's the result of one or more source adapters
having translated their native events into canonical
`StageTransition` rows under a `WorkflowDef`.

Conceptually:

    work_item table   →  Stream.items
    stage_transition  →  Stream.transitions
    workflow schema   →  Stream.workflow

The metric layer asks Stream questions ("what items were in WIP
on this date", "what's the current stage of item X") rather than
asking sources or pattern-matching `item_id` strings. That
discipline is what makes adding a new source (Linear, Asana, …)
a pure adapter — no metric changes.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .canonical import StageTransition, WorkflowDef


@dataclass(frozen=True)
class StreamItem:
    """The canonical work-item row.

    Distinct from `compute.WorkItem` (which carries source-shaped
    fields like `status_intervals` and `activity` for the legacy
    code path). `StreamItem` is the minimal projection the metric
    layer needs once the data lives in canonical form.
    """

    item_id: str
    title: str
    url: str | None
    created_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True)
class Stream:
    items: tuple[StreamItem, ...]
    transitions: tuple[StageTransition, ...]
    workflow: WorkflowDef

    def __init__(
        self,
        *,
        items: Iterable[StreamItem],
        transitions: Iterable[StageTransition],
        workflow: WorkflowDef,
    ) -> None:
        items_t = tuple(items)
        txs_t = tuple(sorted(transitions, key=lambda t: t.entered_at))
        # Validate: every transition's item_id is in items, every
        # transition's stage is in workflow.stages. Bad data caught
        # at construction, not at metric-time.
        known_ids = {i.item_id for i in items_t}
        known_stages = set(workflow.stages)
        for t in txs_t:
            if t.item_id not in known_ids:
                raise ValueError(f"transition references unknown item_id {t.item_id!r}")
            if t.stage not in known_stages:
                raise ValueError(
                    f"transition references unknown stage {t.stage!r}; "
                    f"workflow stages = {workflow.stages}"
                )
        object.__setattr__(self, "items", items_t)
        object.__setattr__(self, "transitions", txs_t)
        object.__setattr__(self, "workflow", workflow)

    def __iter__(self) -> Iterator[StreamItem]:
        return iter(self.items)

    def transitions_for(self, item_id: str) -> Iterator[StageTransition]:
        """Transitions for one item, in chronological order."""
        for t in self.transitions:
            if t.item_id == item_id:
                yield t

    def current_stage_at(self, item_id: str, asof: date) -> str | None:
        """The stage the item occupies on `asof`. Returns None if
        the item hadn't been created yet (no transitions <= asof).
        Uses end-of-day for `asof` so a transition that landed on
        `asof` counts.
        """
        asof_dt = datetime.combine(asof, datetime.max.time()).replace(
            tzinfo=self.transitions[0].entered_at.tzinfo
            if self.transitions
            else None
        )
        last: StageTransition | None = None
        for t in self.transitions_for(item_id):
            if t.entered_at <= asof_dt:
                last = t
            else:
                break
        return last.stage if last is not None else None

    def in_flight_at(self, asof: date) -> list[StreamItem]:
        """Items whose current stage at `asof` is in the workflow's
        WIP set. Completed items still count if their terminal
        transition is itself in `wip_set` (rare; usually it isn't).
        """
        out: list[StreamItem] = []
        wip = self.workflow.wip_set
        for item in self.items:
            stage = self.current_stage_at(item.item_id, asof)
            if stage in wip:
                out.append(item)
        return out


# ----------------------------------------------------------------------
# JSON loader — for tests and the canonical fixture files.
# ----------------------------------------------------------------------

def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_stream_from_json(path: Path) -> Stream:
    """Load a single-workflow Stream from a JSON fixture.

    Fixture schema:
        {
          "workflow": {"stages": [...], "wip_set": [...]},
          "work_items": [{item_id, title, url, created_at, completed_at}, ...],
          "transitions": [{item_id, entered_at, stage, signal}, ...]
        }

    Raises ValueError if the fixture has a multi-workflow shape
    (`workflows` key) — those need an explicit per-item workflow
    map and a richer loader.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    if "workflows" in data:
        raise ValueError(
            "fixture has multiple workflows; the single-workflow loader cannot "
            "interpret it. Use a richer loader that maps each item to its "
            "workflow."
        )
    wf_raw = data["workflow"]
    workflow = WorkflowDef(
        stages=tuple(wf_raw["stages"]),
        wip_set=frozenset(wf_raw["wip_set"]),
    )
    items = [
        StreamItem(
            item_id=i["item_id"],
            title=i["title"],
            url=i.get("url"),
            created_at=_parse_dt(i["created_at"]),
            completed_at=_parse_dt(i["completed_at"])
            if i.get("completed_at")
            else None,
        )
        for i in data["work_items"]
    ]
    transitions = [
        StageTransition(
            item_id=t["item_id"],
            entered_at=_parse_dt(t["entered_at"]),
            stage=t["stage"],
            signal=t["signal"],
        )
        for t in data["transitions"]
    ]
    return Stream(items=items, transitions=transitions, workflow=workflow)
