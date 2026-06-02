"""`flow materialize --bg --at HH:MM` — install a launchd scheduled
job that runs `flow materialize` daily at the chosen local time.

Mirrors the existing `flow serve --bg` plumbing in shape:

  - `render_materialize_plist(...)` — pure XML rendering; tests the
    plist contract with launchd (Label, ProgramArguments,
    StartCalendarInterval, log paths).
  - `install_materialize_schedule(...)` — write plist + idempotent
    bootout-then-bootstrap. subprocess mocked.
  - `stop_materialize_schedule(...)` — bootout + remove plist.
    Idempotent.

All tests use tmp_path and monkeypatch on subprocess — they never
touch the real LaunchAgents dir or invoke launchctl.
"""
from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path


def _capture_subprocess(monkeypatch):
    calls: list[list[str]] = []

    def fake_run(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


class TestRenderMaterializePlist:
    """Pure function. Pin the load-bearing plist keys."""

    def test_renders_a_valid_plist_with_calendar_interval(self):
        from flowmetrics.bg.launchd import render_materialize_plist

        xml = render_materialize_plist(
            label="com.flowmetrics.materialize",
            flow_bin=Path("/Users/me/.local/bin/flow"),
            materialize_args=[
                "--all",
                "--workflows-dir", "/Users/me/flow/config",
                "--data-dir", "/Users/me/flow/data",
            ],
            hour=6,
            minute=0,
            log_dir=Path("/Users/me/flow/data/_status"),
        )
        d = plistlib.loads(xml)

        # Distinct label so this agent doesn't collide with the
        # `flow serve --bg` agent (com.flowmetrics.serve).
        assert d["Label"] == "com.flowmetrics.materialize"

        # ProgramArguments must invoke `flow materialize` with the
        # supplied args (workflows-dir, data-dir, etc.). The launchd
        # spawn is the only place these paths land — they have to be
        # absolute (launchd doesn't inherit a CWD).
        args = d["ProgramArguments"]
        assert args[0] == "/Users/me/.local/bin/flow"
        assert args[1] == "materialize"
        assert "--all" in args
        assert "--workflows-dir" in args
        assert "/Users/me/flow/config" in args
        assert "--data-dir" in args
        assert "/Users/me/flow/data" in args

        # The schedule: 6:00 local time daily. StartCalendarInterval
        # is a dict (one firing) — not RunAtLoad/KeepAlive (that's
        # the serve pattern).
        sched = d["StartCalendarInterval"]
        assert sched["Hour"] == 6
        assert sched["Minute"] == 0
        # We are NOT a long-running service — KeepAlive must not be
        # set (would respawn after the daily run completes).
        assert "KeepAlive" not in d or d["KeepAlive"] is False
        # If the Mac was asleep when 6 AM fires, run on next wake
        # instead of skipping the day.
        assert d.get(
            "StartCalendarIntervalDoesNotFireWhenSleeping"
        ) is False

        # Logs land alongside the serve agent's logs by convention so
        # an operator only watches one directory.
        assert d["StandardOutPath"].endswith("materialize.out.log")
        assert d["StandardErrorPath"].endswith("materialize.err.log")

    def test_supports_single_workflow_name_instead_of_all(self):
        """`flow materialize NAME` (single workflow) is just a
        different positional arg in the scheduled command; the plist
        plumbing doesn't care which mode the user picked."""
        from flowmetrics.bg.launchd import render_materialize_plist

        xml = render_materialize_plist(
            label="com.flowmetrics.materialize",
            flow_bin=Path("/usr/local/bin/flow"),
            materialize_args=[
                "sk",
                "--workflows-dir", "/srv/contracts",
                "--data-dir", "/srv/data",
            ],
            hour=2,
            minute=30,
            log_dir=Path("/srv/data/_status"),
        )
        d = plistlib.loads(xml)
        args = d["ProgramArguments"]
        assert "sk" in args
        assert "--all" not in args
        assert d["StartCalendarInterval"] == {"Hour": 2, "Minute": 30}


class TestInstallMaterializeSchedule:
    """install_and_schedule writes the plist + bootouts any existing
    instance + bootstraps the new one. subprocess mocked."""

    def test_writes_plist_and_bootstraps(self, tmp_path, monkeypatch):
        from flowmetrics.bg.launchd import (
            MATERIALIZE_LABEL,
            install_materialize_schedule,
        )

        calls = _capture_subprocess(monkeypatch)
        launchagents = tmp_path / "LaunchAgents"
        log_dir = tmp_path / "logs"

        plist_path = install_materialize_schedule(
            launchagents_dir=launchagents,
            flow_bin=Path("/Users/me/.local/bin/flow"),
            materialize_args=[
                "--all",
                "--workflows-dir", "/Users/me/flow/config",
                "--data-dir", "/Users/me/flow/data",
            ],
            hour=6, minute=0,
            log_dir=log_dir,
            uid=501,
        )
        assert plist_path.parent == launchagents
        assert plist_path.exists()
        d = plistlib.loads(plist_path.read_bytes())
        assert d["Label"] == MATERIALIZE_LABEL
        # Log directory created (launchd needs the path to exist).
        assert log_dir.is_dir()

        # Bootout first (idempotency), then bootstrap.
        verbs = []
        for c in calls:
            if "bootout" in c:
                verbs.append("bootout")
            elif "bootstrap" in c:
                verbs.append("bootstrap")
        assert verbs == ["bootout", "bootstrap"], verbs

        # Bootstrap targeted gui/<uid> domain with the plist path.
        bootstrap_calls = [c for c in calls if "bootstrap" in c]
        assert len(bootstrap_calls) == 1
        assert "gui/501" in bootstrap_calls[0]
        assert str(plist_path) in bootstrap_calls[0]

    def test_idempotent_reinstall(self, tmp_path, monkeypatch):
        """Re-running `flow materialize --bg --at HH:MM` while
        already installed must reload, not error."""
        from flowmetrics.bg.launchd import install_materialize_schedule

        _capture_subprocess(monkeypatch)
        launchagents = tmp_path / "LaunchAgents"
        launchagents.mkdir()
        (launchagents / "com.flowmetrics.materialize.plist").write_bytes(b"stale")
        log_dir = tmp_path / "logs"

        plist_path = install_materialize_schedule(
            launchagents_dir=launchagents,
            flow_bin=Path("/Users/me/.local/bin/flow"),
            materialize_args=["--all"],
            hour=6, minute=0,
            log_dir=log_dir,
            uid=501,
        )
        # The stale bytes were overwritten with real plist XML.
        assert "<plist" in plist_path.read_text()


class TestStopMaterializeSchedule:
    def test_bootouts_and_removes_plist(self, tmp_path, monkeypatch):
        from flowmetrics.bg.launchd import stop_materialize_schedule

        calls = _capture_subprocess(monkeypatch)
        launchagents = tmp_path / "LaunchAgents"
        launchagents.mkdir()
        plist = launchagents / "com.flowmetrics.materialize.plist"
        plist.write_bytes(b"<plist/>")

        stop_materialize_schedule(
            launchagents_dir=launchagents, uid=501,
        )
        assert any("bootout" in c for c in calls)
        assert not plist.exists()

    def test_idempotent_when_already_gone(self, tmp_path, monkeypatch):
        from flowmetrics.bg.launchd import stop_materialize_schedule

        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], 0),
        )
        launchagents = tmp_path / "LaunchAgents"
        launchagents.mkdir()
        # No plist → must not raise.
        stop_materialize_schedule(
            launchagents_dir=launchagents, uid=501,
        )
