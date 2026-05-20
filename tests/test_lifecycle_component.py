"""Component tests for `flowmetrics.web.components.lifecycle`.

The lifecycle component answers "what happened to *this* item, in
time order?". It reads the `transitions` Parquet (stage entry events
written by `flow materialise`) plus the item's `work_items` row
(for title + URL), and returns a typed payload the Jinja partial
renders as a Vega-Lite timeline.

The contract:

  - Identity: (contract_id, source, item_id) selects exactly one item.
    Missing identity → ItemNotFound (component layer; route maps to 404).
  - Events: every transition for the item, sorted ascending by
    entered_at. The first event is the "Opened" entry; the last is
    typically the terminal entry (Merged / Done).
  - Durations: each event carries its dwell time in the prior
    stage (None for the first event).
  - Dates: every datetime crosses the warehouse-read boundary via
    `flowmetrics.utc_dates` to stay TZ-invariant.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.lifecycle import ItemNotFound, render

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    tmp = Path(tempfile.mkdtemp())
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"
    (contracts_dir / "astral-uv-week.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
                    "name": "astral-uv-week",
                    "source": "github",
                    "repo": "astral-sh/uv",
                    "start": "2026-05-04",
                    "stop": "2026-05-10",
                }
            }
        )
    )
    res = CliRunner().invoke(
        cli,
        [
            "materialise",
            "astral-uv-week",
            "--data-dir",
            str(data_dir),
            "--contracts-dir",
            str(contracts_dir),
            "--cache-dir",
            str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output

    con = duckdb.connect(":memory:")
    for kind, name in [("work_items", "work_items"), ("transitions", "transitions")]:
        glob = (data_dir / kind / "**" / "*.parquet").as_posix()
        con.execute(
            f"CREATE VIEW {name} AS "
            f"SELECT * FROM read_parquet('{glob}', hive_partitioning = true)"
        )
    yield con
    con.close()


@pytest.fixture
def known_item(warehouse) -> tuple[str, str]:
    """Return (source, item_id) for an item that exists in the
    fixture. We don't hardcode an id — pick the first."""
    row = warehouse.execute(
        "SELECT source, item_id FROM work_items "
        "WHERE contract_id = 'astral-uv-week' "
        "ORDER BY completed_at DESC LIMIT 1"
    ).fetchone()
    assert row, "fixture must have at least one work item"
    return row[0], row[1]


@pytest.fixture
def synth_warehouse() -> duckdb.DuckDBPyConnection:
    """In-memory warehouse with a single synthetic item whose
    transitions extend PAST completed_at — exactly the live-data
    case where a PR is re-labeled after merge. Lets us exercise
    the post-completion truncation logic without depending on the
    offline fixture cache (which doesn't include this case)."""
    from datetime import datetime as _dt
    con = duckdb.connect(":memory:")
    con.execute(
        """
        CREATE TABLE work_items (
          contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
          title VARCHAR, url VARCHAR,
          created_at TIMESTAMP, completed_at TIMESTAMP,
          cycle_time_days DOUBLE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE transitions (
          contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
          entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR
        )
        """
    )
    con.execute(
        "INSERT INTO work_items VALUES "
        "('demo', 'github', '#1', 'Test', 'https://example.com',"
        " '2026-05-06 16:00:00', '2026-05-07 08:00:00', 2.0)"
    )
    # Transitions: 4 in-cycle + 1 post-completion.
    con.execute(
        "INSERT INTO transitions VALUES "
        "('demo', 'github', '#1', '2026-05-06 16:00:00', 'Draft', 'github-pr-created'),"
        "('demo', 'github', '#1', '2026-05-06 18:00:00', 'Awaiting Review', 'github-label-added'),"
        "('demo', 'github', '#1', '2026-05-06 22:00:00', 'Approved', 'github-label-added'),"
        "('demo', 'github', '#1', '2026-05-07 08:00:00', 'Merged', 'github-pr-merged'),"
        "('demo', 'github', '#1', '2026-05-09 16:00:00', 'Approved', 'github-label-added')"
    )
    # Also add an in-flight item (no completed_at) for the
    # "keep all events" test.
    con.execute(
        "INSERT INTO work_items VALUES "
        "('demo', 'github', '#2', 'Open', 'https://example.com',"
        " '2026-05-06 10:00:00', NULL, NULL)"
    )
    con.execute(
        "INSERT INTO transitions VALUES "
        "('demo', 'github', '#2', '2026-05-06 10:00:00', 'Draft', 'github-pr-created'),"
        "('demo', 'github', '#2', '2026-05-08 12:00:00', 'Awaiting Review', 'github-label-added'),"
        "('demo', 'github', '#2', '2026-05-10 09:00:00', 'Changes Requested', 'github-label-added')"
    )
    yield con
    con.close()


class TestLifecyclePostCompletionTruncation:
    """Post-merge label changes can land in the transitions
    table — e.g., someone re-tags a PR days after merging. These
    events don't change cycle time (which uses created_at /
    completed_at), but they extend the lifecycle visualization
    PAST completion with a fake "Merged · 2d 8h" dwell that
    misleads the viewer into thinking the cycle is longer than
    it is.

    Pin: when the work_items row says completed_at = T, the
    lifecycle's stages must end at or before T. Any transition
    rows with entered_at > T are post-completion noise and
    don't belong on the cycle-view timeline.
    """

    def test_stages_dont_extend_past_completed_at(self, synth_warehouse):
        """Item #1 has 5 transitions: 4 in-cycle (Draft, Awaiting
        Review, Approved, Merged at 08:00 on May 7) + 1 post-
        completion (Approved at May 9 16:00 — 2 days later).
        The lifecycle stages must end at or before completed_at
        (May 7 08:00); the post-completion transition is
        polluting noise and must not show up as a "Merged · 2d"
        dwell."""
        from flowmetrics.web.components.lifecycle import render
        data = render(synth_warehouse, "demo", "github", "#1")
        completed_iso = "2026-05-07T08:00:00"
        for stage in data.stages:
            assert stage.exited_at_iso[:19] <= completed_iso, (
                f"stage {stage.stage!r} exits at "
                f"{stage.exited_at_iso} which is AFTER completed_at "
                f"({completed_iso}) — post-completion transitions are "
                f"polluting the lifecycle view. Truncate at "
                f"completed_at when building stages."
            )
        # And the last stage's exit MUST be exactly completed_at
        # (the Merged event), not the spurious post-merge re-tag.
        assert data.stages[-1].exited_at_iso[:19] == completed_iso, (
            f"last stage should exit at completed_at; got "
            f"{data.stages[-1].exited_at_iso}"
        )

    def test_terminal_transition_with_microsecond_offset_is_kept(
        self, synth_warehouse
    ):
        """Real-world data: the completion event's transition can
        land microseconds AFTER the work_item's completed_at
        timestamp (the two fields come from separate API calls
        with mismatched precision — Jira returns one at second
        resolution, the other at ms). A strict `entered_at <=
        completed_at` truncation drops the very event that marked
        completion, leaving the lifecycle with 0 stages — and a
        crash in the summary template that assumes stages[0].

        Compare at second-precision instead. Pin a synthetic item
        where the resolved transition is 37µs after completed_at;
        the lifecycle must still record 2 events / 1 stage."""
        from datetime import datetime as _dt
        synth_warehouse.execute(
            "INSERT INTO work_items VALUES "
            "('demo', 'jira', '#x1', 'Test', NULL,"
            " '2024-12-27 05:43:25', '2025-04-05 05:01:30', 100.0)"
        )
        synth_warehouse.execute(
            "INSERT INTO transitions VALUES "
            "('demo', 'jira', '#x1', '2024-12-27 05:43:25', "
            "'Triage Needed', 'jira-issue-created'),"
            "('demo', 'jira', '#x1', '2025-04-05 05:01:30.037', "
            "'Resolved', 'jira-resolved')"
        )
        from flowmetrics.web.components.lifecycle import render
        data = render(synth_warehouse, "demo", "jira", "#x1")
        assert len(data.events) == 2, (
            f"completion-event transition with microsecond offset "
            f"must NOT be truncated; got {len(data.events)} events"
        )
        assert len(data.stages) == 1
        assert data.stages[0].stage == "Triage Needed"

    def test_in_flight_items_keep_all_events(self, synth_warehouse):
        """Truncation only applies to COMPLETED items. Item #2
        has completed_at=NULL and 3 transitions — all 3 must
        appear in the lifecycle view."""
        from flowmetrics.web.components.lifecycle import render
        data = render(synth_warehouse, "demo", "github", "#2")
        assert len(data.events) == 3, (
            f"in-flight item should keep all 3 transitions; got "
            f"{len(data.events)}"
        )


class TestLifecycleShape:
    def test_returns_events_in_ascending_time_order(self, warehouse, known_item):
        source, item_id = known_item
        data = render(warehouse, "astral-uv-week", source, item_id)
        assert data.events, "expected at least one event"
        entered = [e.entered_at_iso for e in data.events]
        assert entered == sorted(entered), (
            f"events must be ascending by entered_at; got {entered}"
        )

    def test_includes_item_identity_header(self, warehouse, known_item):
        """The header carries the item id, title, URL, and source —
        everything the page chrome needs without re-querying."""
        source, item_id = known_item
        data = render(warehouse, "astral-uv-week", source, item_id)
        assert data.item_id == item_id
        assert data.source == source
        assert data.title, "title must be present"
        assert data.url is None or data.url.startswith("http"), (
            f"url must be None or a real URL; got {data.url!r}"
        )

    def test_each_event_carries_stage_signal_and_iso_date(
        self, warehouse, known_item
    ):
        source, item_id = known_item
        data = render(warehouse, "astral-uv-week", source, item_id)
        first = data.events[0]
        assert first.stage, "stage must be present"
        assert first.signal, "signal must be present"
        # ISO-8601 UTC datetime, e.g. 2026-05-10T17:19:39Z
        assert re.match(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
            first.entered_at_iso,
        ), f"entered_at_iso must be UTC ISO-8601 with Z suffix; got {first.entered_at_iso!r}"

    def test_first_event_has_no_dwell_time(self, warehouse, known_item):
        """Nothing happened before the first transition, so no
        prior-stage dwell. Renders as None / blank."""
        source, item_id = known_item
        data = render(warehouse, "astral-uv-week", source, item_id)
        assert data.events[0].dwell_days is None

    def test_later_events_have_non_negative_dwell(self, warehouse, known_item):
        """Each subsequent event's dwell = (this.entered - prev.entered)
        in days, never negative."""
        source, item_id = known_item
        data = render(warehouse, "astral-uv-week", source, item_id)
        for e in data.events[1:]:
            assert e.dwell_days is not None
            assert e.dwell_days >= 0, (
                f"event {e.stage} reported negative dwell {e.dwell_days}d"
            )

    def test_missing_item_raises_itemnotfound(self, warehouse):
        with pytest.raises(ItemNotFound):
            render(
                warehouse, "astral-uv-week", "github", "#does-not-exist"
            )

    def test_stages_pair_consecutive_events(self, warehouse):
        """A 3-event lifecycle (Draft → Awaiting Review → Merged)
        produces 2 stages: Draft [event0, event1], Awaiting Review
        [event1, event2]. The terminal event (Merged) is not its
        own stage — it's just the exit time of the last stage."""
        # PR #19342 in the fixture has 3 events.
        data = render(warehouse, "astral-uv-week", "github", "#19342")
        # Verify the test premise: this item has 3 events.
        assert len(data.events) == 3
        # And 2 stages (n_events - 1 pairing).
        assert len(data.stages) == 2
        stage_names = [s.stage for s in data.stages]
        assert stage_names == ["Draft", "Awaiting Review"], (
            f"stages should pair consecutive events; got {stage_names}"
        )

    def test_stage_exit_matches_next_event_entry(self, warehouse):
        """A stage's exited_at equals the next event's entered_at —
        that's what "stage transition" means."""
        data = render(warehouse, "astral-uv-week", "github", "#19342")
        first_stage = data.stages[0]
        second_event = data.events[1]
        assert first_stage.exited_at_iso == second_event.entered_at_iso
        # The terminal event's stage is the LAST stage's name (the
        # state the item exited into), exposed for display.
        terminal_event = data.events[-1]
        assert data.terminal_stage == terminal_event.stage

    def test_stage_duration_matches_event_gap(self, warehouse):
        """duration_seconds == (next.entered_at - this.entered_at)."""
        data = render(warehouse, "astral-uv-week", "github", "#19342")
        s = data.stages[0]
        e0, e1 = data.events[0], data.events[1]
        # Re-parse the ISO strings to seconds and compare.
        from datetime import datetime

        def _parse(s: str) -> datetime:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))

        expected = (_parse(e1.entered_at_iso) - _parse(e0.entered_at_iso)).total_seconds()
        assert abs(s.duration_seconds - expected) < 0.5

    def test_stage_y_label_combines_stage_and_duration(self, warehouse):
        """The gantt chart's y-axis label shows `<stage> · <duration>`
        so the duration sits ALWAYS-VISIBLE on the y-axis (not as an
        in-bar text overlay which gets cut off for narrow bars and
        overflows the chart for short stages). Test the canonical
        form."""
        data = render(warehouse, "astral-uv-week", "github", "#19342")
        for s in data.stages:
            assert s.y_label == f"{s.stage} · {s.duration_display}", (
                f"y_label must combine stage and duration as "
                f"'<stage> · <duration>'; got {s.y_label!r}"
            )

    def test_duration_display_is_human_readable(self, warehouse):
        """Stage durations render as `Xh Ym Zs` (or shorter for sub-hour),
        not raw seconds."""
        import re

        data = render(warehouse, "astral-uv-week", "github", "#19342")
        for s in data.stages:
            assert re.match(
                r"^(\d+h ?)?(\d+m ?)?\d+s$|^\d+m \d+s$|^\d+s$|^\d+h \d+m$|^\d+d \d+h$",
                s.duration_display.strip(),
            ), (
                f"duration_display must be a human-readable string "
                f"like '37m 39s' or '2h 5m' or '1d 4h'; got "
                f"{s.duration_display!r}"
            )

    def test_two_event_lifecycle_is_not_chartable(self, warehouse):
        """User feedback: when an item has only a start and an end
        (e.g. PR opened directly into Awaiting Review and merged),
        a "timeline chart" is noise — there's nothing to compare.
        The component flags this with `is_chartable = False` so the
        view can render a summary card instead of a chart."""
        # PR #19303 in the fixture has exactly 2 events (created
        # straight into Awaiting Review, then Merged).
        data = render(warehouse, "astral-uv-week", "github", "#19303")
        assert len(data.events) == 2
        assert len(data.stages) == 1
        assert data.is_chartable is False

    def test_three_or_more_event_lifecycle_is_chartable(self, warehouse):
        """With 2+ stages the gantt is informative — different stages
        consume different proportions of the cycle, which is the
        point of the chart."""
        data = render(warehouse, "astral-uv-week", "github", "#19342")
        assert len(data.stages) >= 2
        assert data.is_chartable is True

    def test_stages_json_for_vega_carries_required_fields(self, warehouse):
        import json

        data = render(warehouse, "astral-uv-week", "github", "#19342")
        parsed = json.loads(data.stages_json)
        assert parsed, "stages_json must contain at least one stage"
        required = {
            "stage",
            "entered_at_iso",
            "exited_at_iso",
            "duration_seconds",
            "duration_display",
        }
        assert required <= set(parsed[0]), (
            f"stage dict missing required keys; got keys: "
            f"{sorted(parsed[0])}"
        )

    def test_payload_carries_cycle_time_days_matching_work_items(
        self, warehouse
    ):
        """The lifecycle page must surface the SAME cycle-time
        number the work-items table reports — Vacanti's
        `elapsed + 1 day`, not just the chart's wall-clock span.
        Otherwise a viewer compares "1.41 in the table" with
        "~1/3 day on the chart" and reasonably asks which is real.

        Both must agree. Test by querying work_items directly for
        the canonical value and asserting the lifecycle payload
        matches."""
        item_id = "#19330"
        data = render(warehouse, "astral-uv-week", "github", item_id)
        canonical = warehouse.execute(
            "SELECT cycle_time_days FROM work_items "
            "WHERE contract_id = ? AND source = ? AND item_id = ? "
            "LIMIT 1",
            ["astral-uv-week", "github", item_id],
        ).fetchone()[0]
        assert data.cycle_time_days == canonical, (
            f"lifecycle.cycle_time_days must equal the work_items "
            f"row's cycle_time_days; got {data.cycle_time_days!r} "
            f"vs {canonical!r}"
        )

    def test_payload_carries_human_readable_elapsed(self, warehouse):
        """Two views of duration on the lifecycle page:
          - `cycle_time_days` — the Vacanti metric (elapsed + 1)
          - `elapsed_display` — the actual wall-clock span ("9h 53m")
        Surfacing both lets the viewer reconcile the chart's time
        axis with the metric used everywhere else."""
        data = render(warehouse, "astral-uv-week", "github", "#19330")
        # #19330's wall-clock span is 9h 53m (see fixture).
        # The display string follows the lifecycle component's
        # `_duration_display` formatting.
        assert data.elapsed_display, "elapsed_display must be set"
        assert "h" in data.elapsed_display or "m" in data.elapsed_display, (
            f"elapsed_display should be `Xh Ym`-style; got "
            f"{data.elapsed_display!r}"
        )

    def test_payload_is_jsonable_for_vega_lite(self, warehouse, known_item):
        """The events list serialises to a flat list of dicts that
        Vega-Lite can consume directly as inline data. The component
        exposes `events_json` as the ready-to-embed JSON literal."""
        import json

        source, item_id = known_item
        data = render(warehouse, "astral-uv-week", source, item_id)
        assert data.events_json, "events_json must be a non-empty string"
        parsed = json.loads(data.events_json)
        assert isinstance(parsed, list)
        assert parsed, "events_json must contain at least one event"
        assert {"stage", "entered_at_iso", "entered_at_display"} <= set(
            parsed[0]
        ), f"event dict missing required keys; got keys: {sorted(parsed[0])}"
