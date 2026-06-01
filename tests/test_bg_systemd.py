"""`flow serve --bg` — Linux systemd-user implementation.

Pins three things, same shape as the launchd tests:

  1. The unit file we render has the load-bearing INI sections —
     [Unit], [Service], [Install] — and Restart=on-failure so the
     dashboard comes back after a crash.

  2. install_and_start writes the unit to the systemd user dir,
     runs daemon-reload, enables + restarts the service.
     subprocess is mocked.

  3. stop_and_uninstall disables + stops the service AND removes
     the unit file, then daemon-reloads. Idempotent when nothing's
     there.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


class TestRenderServeUnit:
    def test_renders_a_systemd_unit_with_load_bearing_sections(self):
        from flowmetrics.bg import systemd

        unit = systemd.render_serve_unit(
            flow_bin=Path("/home/me/.local/bin/flow"),
            workflows_dir=Path("/home/me/flow/contracts"),
            data_dir=Path("/home/me/flow/data"),
            port=8000,
            host="127.0.0.1",
            password=None,
            log_dir=Path("/home/me/flow/data/_status"),
        )
        # INI section headers — systemd refuses to load a unit
        # missing [Service]; [Unit] + [Install] are conventional
        # but [Install] is load-bearing because we `enable` the
        # unit.
        assert "[Unit]" in unit
        assert "[Service]" in unit
        assert "[Install]" in unit
        # ExecStart carries the resolved flow binary + chosen
        # flags. Each line of the unit terminates with a real
        # newline (systemd's parser is line-oriented).
        assert "ExecStart=/home/me/.local/bin/flow serve" in unit
        assert "--workflows-dir /home/me/flow/contracts" in unit
        assert "--data-dir /home/me/flow/data" in unit
        assert "--port 8000" in unit
        assert "--host 127.0.0.1" in unit
        # Persistence: respawn on crash.
        assert "Restart=on-failure" in unit
        # Logs land where the launchd path puts them, for parity
        # so docs can name one path on both OSes.
        assert "/home/me/flow/data/_status/serve.out.log" in unit
        assert "/home/me/flow/data/_status/serve.err.log" in unit
        # Working dir set explicitly (systemd doesn't inherit
        # CWD).
        assert "WorkingDirectory=" in unit
        # User-unit lifecycle: enable+now → start at login; with
        # `loginctl enable-linger`, survives logout. The Install
        # target makes that work.
        assert "WantedBy=default.target" in unit

    def test_omits_password_arg_when_none_for_loopback(self):
        from flowmetrics.bg import systemd

        unit = systemd.render_serve_unit(
            flow_bin=Path("/usr/local/bin/flow"),
            workflows_dir=Path("/srv/contracts"),
            data_dir=Path("/srv/data"),
            port=8000,
            host="127.0.0.1",
            password=None,
            log_dir=Path("/srv/data/_status"),
        )
        assert "--password" not in unit

    def test_includes_password_arg_when_set(self):
        from flowmetrics.bg import systemd

        unit = systemd.render_serve_unit(
            flow_bin=Path("/usr/local/bin/flow"),
            workflows_dir=Path("/srv/contracts"),
            data_dir=Path("/srv/data"),
            port=8000,
            host="0.0.0.0",
            password="hunter2",
            log_dir=Path("/srv/data/_status"),
        )
        assert "--password hunter2" in unit
        assert "--host 0.0.0.0" in unit


class TestInstallAndStart:
    def _capture_subprocess(self, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)
        return calls

    def test_writes_unit_to_user_systemd_dir(self, tmp_path, monkeypatch):
        from flowmetrics.bg import systemd

        calls = self._capture_subprocess(monkeypatch)
        unit_dir = tmp_path / ".config" / "systemd" / "user"
        log_dir = tmp_path / "logs"

        unit_path = systemd.install_and_start(
            unit_dir=unit_dir,
            flow_bin=Path("/home/me/.local/bin/flow"),
            workflows_dir=Path("/home/me/flow/contracts"),
            data_dir=Path("/home/me/flow/data"),
            port=8000,
            host="127.0.0.1",
            password=None,
            log_dir=log_dir,
        )
        # File on disk, in the user systemd dir.
        assert unit_path.parent == unit_dir
        assert unit_path.name == "flowmetrics-serve.service"
        assert unit_path.exists()
        # Has the [Service] header at minimum (full render is
        # pinned by the render tests above).
        assert "[Service]" in unit_path.read_text()
        # Log dir was created so systemd's StandardOutput append
        # has a target.
        assert log_dir.is_dir()
        # systemctl was invoked: daemon-reload, then enable, then
        # restart. The exact order matters — without
        # daemon-reload first, enabling a freshly-written unit
        # silently picks up the old (or no) version.
        verbs = []
        for c in calls:
            if "daemon-reload" in c:
                verbs.append("daemon-reload")
            elif "enable" in c and any("flowmetrics-serve" in p for p in c):
                verbs.append("enable")
            elif "restart" in c and any("flowmetrics-serve" in p for p in c):
                verbs.append("restart")
        assert verbs == ["daemon-reload", "enable", "restart"], verbs
        # Every systemctl call is `--user` (no root needed for the
        # user manager).
        for c in calls:
            assert "systemctl" in c[0]
            assert "--user" in c

    def test_idempotent_reinstall(self, tmp_path, monkeypatch):
        """Re-running `flow serve --bg` against an already-installed
        service must not error — daemon-reload + enable + restart
        is the correct idempotent sequence even when the unit is
        already running."""
        from flowmetrics.bg import systemd

        self._capture_subprocess(monkeypatch)
        unit_dir = tmp_path / ".config" / "systemd" / "user"
        log_dir = tmp_path / "logs"

        # Pretend the agent was already installed.
        unit_dir.mkdir(parents=True)
        (unit_dir / "flowmetrics-serve.service").write_text("# stale")

        systemd.install_and_start(
            unit_dir=unit_dir,
            flow_bin=Path("/home/me/.local/bin/flow"),
            workflows_dir=Path("/home/me/flow/contracts"),
            data_dir=Path("/home/me/flow/data"),
            port=8000,
            host="127.0.0.1",
            password=None,
            log_dir=log_dir,
        )
        # The file was overwritten with fresh content (not the
        # "# stale" placeholder).
        assert "[Service]" in (unit_dir / "flowmetrics-serve.service").read_text()


class TestStopAndUninstall:
    def test_disables_stops_then_removes_unit(self, tmp_path, monkeypatch):
        from flowmetrics.bg import systemd

        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(list(cmd))
            return subprocess.CompletedProcess(cmd, returncode=0)

        monkeypatch.setattr(subprocess, "run", fake_run)

        unit_dir = tmp_path / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        unit = unit_dir / "flowmetrics-serve.service"
        unit.write_text("[Unit]\n...")

        systemd.stop_and_uninstall(unit_dir=unit_dir)

        # systemctl disable --now (one call) stops + disables.
        disable_calls = [
            c for c in calls
            if "disable" in c and any("flowmetrics-serve" in p for p in c)
        ]
        assert disable_calls
        # The unit file was removed.
        assert not unit.exists()
        # daemon-reload AFTER removal so systemd forgets the unit.
        assert any("daemon-reload" in c for c in calls)

    def test_uninstall_is_noop_when_already_gone(self, tmp_path, monkeypatch):
        from flowmetrics.bg import systemd

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **kw: subprocess.CompletedProcess(a[0], 0),
        )
        unit_dir = tmp_path / ".config" / "systemd" / "user"
        unit_dir.mkdir(parents=True)
        # No unit file → no error.
        systemd.stop_and_uninstall(unit_dir=unit_dir)
