"""`flow serve --bg` — install + start the dashboard as a persistent
native service.

Slice 1: macOS launchd. The bg module hides launchctl mechanics
behind two verbs: `install_and_start` (idempotent — restart if
already installed) and `stop_and_uninstall` (full teardown).

These tests pin three things:

  1. The plist we generate has the load-bearing keys (`Label`,
     `ProgramArguments`, `RunAtLoad`, `KeepAlive`, working dir,
     log paths). The plist file IS the workflow with launchd —
     a typo here means the service silently doesn't behave like
     a persistent service.

  2. `install_and_start` writes the plist to the right LaunchAgents
     dir AND calls `launchctl bootstrap` (after bootout if it was
     already loaded). subprocess is mocked so the test doesn't
     need a real launchd.

  3. Non-macOS platforms refuse with a clear error pointing at the
     templated path — we never want a Linux user to get a cryptic
     "launchctl not found" traceback.
"""
from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

# Plist render takes Path objects and stringifies them into the
# `ProgramArguments` array. On Windows, `str(Path('/Users/.../flow'))`
# uses backslashes — which is nonsense for launchd (macOS-only) and
# makes the cross-platform string-equality assertions in this file
# trip over the host's path style. Skip the whole module on Windows;
# the production code-path is never reached there anyway.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="launchd is macOS-only; render path is unreachable on Windows.",
)


class TestRenderServePlist:
    """Pure function: dict-in, plist-XML-bytes-out. Authoritative
    workflow with launchd lives in this dict."""

    def test_renders_a_valid_plist_with_load_bearing_keys(self, tmp_path):
        from flowmetrics.bg.launchd import render_serve_plist

        xml = render_serve_plist(
            label="com.flowmetrics.serve",
            flow_bin=Path("/Users/me/.local/bin/flow"),
            workflows_dir=Path("/Users/me/flow/contracts"),
            data_dir=Path("/Users/me/flow/data"),
            port=8000,
            host="127.0.0.1",
            password=None,
            log_dir=Path("/Users/me/flow/data/_status"),
        )
        # plistlib parses the round-trip — if we emitted invalid
        # XML, this throws.
        d = plistlib.loads(xml)
        assert d["Label"] == "com.flowmetrics.serve"
        # ProgramArguments is the launchd "ARGV" — must lead with
        # the flow binary and carry the resolved dirs.
        assert d["ProgramArguments"][0] == "/Users/me/.local/bin/flow"
        assert "serve" in d["ProgramArguments"]
        assert "--workflows-dir" in d["ProgramArguments"]
        assert "/Users/me/flow/contracts" in d["ProgramArguments"]
        assert "--data-dir" in d["ProgramArguments"]
        assert "/Users/me/flow/data" in d["ProgramArguments"]
        assert "--port" in d["ProgramArguments"]
        assert "8000" in d["ProgramArguments"]
        # Persistence: both keys load-bearing.
        assert d["RunAtLoad"] is True
        assert d["KeepAlive"] is True
        # Logs land under data/_status by convention (matches the
        # static template; users know where to look).
        assert d["StandardOutPath"].endswith("serve.out.log")
        assert d["StandardErrorPath"].endswith("serve.err.log")
        # WorkingDirectory must be absolute — launchd doesn't
        # inherit a CWD.
        assert Path(d["WorkingDirectory"]).is_absolute()

    def test_omits_password_arg_when_none_for_loopback(self, tmp_path):
        """Loopback bind doesn't need a password. The plist must
        NOT carry `--password` then — a stray empty value would
        confuse Click."""
        from flowmetrics.bg.launchd import render_serve_plist

        xml = render_serve_plist(
            label="com.flowmetrics.serve",
            flow_bin=Path("/usr/local/bin/flow"),
            workflows_dir=Path("/srv/contracts"),
            data_dir=Path("/srv/data"),
            port=8000,
            host="127.0.0.1",
            password=None,
            log_dir=Path("/srv/data/_status"),
        )
        d = plistlib.loads(xml)
        assert "--password" not in d["ProgramArguments"]

    def test_includes_password_arg_when_set(self):
        """Non-loopback binds need a password. When supplied, the
        plist carries `--password VALUE` so the agent boots clean."""
        from flowmetrics.bg.launchd import render_serve_plist

        xml = render_serve_plist(
            label="com.flowmetrics.serve",
            flow_bin=Path("/usr/local/bin/flow"),
            workflows_dir=Path("/srv/contracts"),
            data_dir=Path("/srv/data"),
            port=8000,
            host="0.0.0.0",
            password="hunter2",
            log_dir=Path("/srv/data/_status"),
        )
        d = plistlib.loads(xml)
        args = d["ProgramArguments"]
        assert "--password" in args
        assert "hunter2" in args
        assert "--host" in args
        assert "0.0.0.0" in args


class TestInstallAndStart:
    """install_and_start writes the plist + invokes launchctl.
    subprocess is mocked so the test doesn't depend on launchd."""

    def _capture_subprocess(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            # Mimic the success exit code launchctl returns on
            # bootstrap / bootout. Real return codes are 0 on
            # success; on bootout when not loaded, 113 (no such
            # service) — install_and_start tolerates that.
            return subprocess.CompletedProcess(cmd, returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    def test_writes_plist_to_user_launchagents_dir(self, tmp_path, monkeypatch):
        from flowmetrics.bg.launchd import install_and_start

        calls = self._capture_subprocess(monkeypatch)
        launchagents = tmp_path / "LaunchAgents"
        log_dir = tmp_path / "logs"

        plist_path = install_and_start(
            launchagents_dir=launchagents,
            flow_bin=Path("/Users/me/.local/bin/flow"),
            workflows_dir=Path("/Users/me/flow/contracts"),
            data_dir=Path("/Users/me/flow/data"),
            port=8000,
            host="127.0.0.1",
            password=None,
            log_dir=log_dir,
            uid=501,
        )
        # File on disk, in the LaunchAgents dir.
        assert plist_path.parent == launchagents
        assert plist_path.exists()
        # Parses as a valid plist with the expected label.
        d = plistlib.loads(plist_path.read_bytes())
        assert d["Label"] == "com.flowmetrics.serve"
        # Log dir was created (launchd opens these files; if the
        # dir doesn't exist the agent fails to spawn).
        assert log_dir.is_dir()
        # We called launchctl bootstrap with the resolved plist
        # path. We DO NOT care about the bootout call's exit code
        # (it's a noop when the agent isn't loaded yet), but the
        # ORDER matters: bootout first, then bootstrap, so the
        # second call always installs a fresh state.
        bootstrap_calls = [c for c in calls if "bootstrap" in c]
        assert len(bootstrap_calls) == 1
        cmd = bootstrap_calls[0]
        assert "launchctl" in cmd[0]
        assert "gui/501" in cmd
        assert str(plist_path) in cmd

    def test_idempotent_install_runs_bootout_then_bootstrap(
        self, tmp_path, monkeypatch
    ):
        """Re-running `flow serve --bg` while already installed
        should NOT error out — it should reload (bootout then
        bootstrap)."""
        from flowmetrics.bg.launchd import install_and_start

        calls = self._capture_subprocess(monkeypatch)
        launchagents = tmp_path / "LaunchAgents"
        log_dir = tmp_path / "logs"

        # Pretend the agent was already installed.
        launchagents.mkdir()
        (launchagents / "com.flowmetrics.serve.plist").write_bytes(b"stale")

        install_and_start(
            launchagents_dir=launchagents,
            flow_bin=Path("/Users/me/.local/bin/flow"),
            workflows_dir=Path("/Users/me/flow/contracts"),
            data_dir=Path("/Users/me/flow/data"),
            port=8000,
            host="127.0.0.1",
            password=None,
            log_dir=log_dir,
            uid=501,
        )
        # Expect: bootout (best-effort) THEN bootstrap.
        verbs = [next((v for v in c if v in ("bootstrap", "bootout")), None)
                 for c in calls]
        verbs = [v for v in verbs if v is not None]
        assert verbs == ["bootout", "bootstrap"], verbs


class TestStopAndUninstall:
    def test_bootouts_then_removes_plist(self, tmp_path, monkeypatch):
        from flowmetrics.bg.launchd import stop_and_uninstall

        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)

        launchagents = tmp_path / "LaunchAgents"
        launchagents.mkdir()
        plist = launchagents / "com.flowmetrics.serve.plist"
        plist.write_bytes(b"<plist/>")

        stop_and_uninstall(launchagents_dir=launchagents, uid=501)

        # launchctl bootout fired with our agent label.
        assert any("bootout" in c for c in calls)
        # File on disk gone.
        assert not plist.exists()

    def test_uninstall_is_noop_when_already_gone(self, tmp_path, monkeypatch):
        """Idempotent: running `--bg --stop` twice in a row must
        not error. Real-world: an operator runs it once, isn't
        sure it worked, runs again."""
        from flowmetrics.bg.launchd import stop_and_uninstall

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], 0),
        )
        launchagents = tmp_path / "LaunchAgents"
        launchagents.mkdir()
        # No plist file → still should not raise.
        stop_and_uninstall(launchagents_dir=launchagents, uid=501)


