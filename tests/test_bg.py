"""Dispatcher for `flow serve --bg` — picks launchd vs systemd vs
"unsupported" off `sys.platform`.

Platform-specific behaviour lives in `bg/launchd.py` and
`bg/systemd.py` and is covered by its own test file. Here we only
pin the routing: each platform string lands at the right module and
unknown platforms raise BgError with an actionable message.
"""
from __future__ import annotations

from pathlib import Path

import pytest


class TestPlatformRouting:
    """We monkeypatch `sys.platform` AND replace the platform-specific
    `install_and_start` / `stop_and_uninstall` with spies so the
    test asserts on dispatch behaviour without needing the actual
    launchctl / systemctl binaries available."""

    def _common_args(self, tmp_path):
        return {
            "flow_bin": Path("/usr/local/bin/flow"),
            "workflows_dir": Path("/srv/contracts"),
            "data_dir": Path("/srv/data"),
            "port": 8000,
            "host": "127.0.0.1",
            "password": None,
            "log_dir": tmp_path / "logs",
        }

    def test_macos_routes_to_launchd(self, tmp_path, monkeypatch):
        import flowmetrics.bg as bg
        from flowmetrics.bg import launchd

        monkeypatch.setattr("sys.platform", "darwin")
        captured = {}

        def fake_install(*, launchagents_dir, uid, **rest):
            captured["called"] = "launchd"
            captured["uid"] = uid
            return launchagents_dir / "fake.plist"

        monkeypatch.setattr(launchd, "install_and_start", fake_install)
        monkeypatch.setattr(launchd, "current_uid", lambda: 501)

        bg.install_and_start(**self._common_args(tmp_path))
        assert captured["called"] == "launchd"
        assert captured["uid"] == 501

    def test_linux_routes_to_systemd(self, tmp_path, monkeypatch):
        import flowmetrics.bg as bg
        from flowmetrics.bg import systemd

        monkeypatch.setattr("sys.platform", "linux")
        captured = {}

        def fake_install(*, unit_dir, **rest):
            captured["called"] = "systemd"
            captured["unit_dir"] = unit_dir
            return unit_dir / "fake.service"

        monkeypatch.setattr(systemd, "install_and_start", fake_install)
        monkeypatch.setattr(
            systemd,
            "default_user_unit_dir",
            lambda: tmp_path / ".config" / "systemd" / "user",
        )

        bg.install_and_start(**self._common_args(tmp_path))
        assert captured["called"] == "systemd"

    def test_unsupported_platform_raises_bg_error_with_pointer(
        self, tmp_path, monkeypatch
    ):
        """Windows + BSD + anything we don't carry a native unit for
        must surface a clear pointer at the templated path under
        scripts/scheduling/ — never a `command not found` from the
        wrong service manager."""
        import flowmetrics.bg as bg

        monkeypatch.setattr("sys.platform", "win32")
        with pytest.raises(bg.BgError) as exc:
            bg.install_and_start(**self._common_args(tmp_path))
        msg = str(exc.value)
        assert "scripts/scheduling" in msg
        assert "macOS" in msg or "Linux" in msg
