"""Cross-platform compatibility for the bits that touch the OS.

Two surfaces are platform-conditional:

1. The detached subprocess used by the browser-triggered backfill in
   `app.py`. POSIX wants `start_new_session=True`; Windows wants
   `creationflags=CREATE_NEW_PROCESS_GROUP`. Passing the POSIX flag on
   Windows raises immediately, so the wrong branch is the kind of bug
   that only shows up on the platform the developer doesn't have.

2. The port-busy error message in `cli.py` (and `samples` `serve.py`).
   `lsof` / `kill` are POSIX-only; Windows users need
   `netstat -ano | findstr :PORT` and `taskkill /F /PID PID`. A user
   pasting the suggested command shouldn't get "command not found".

Both surfaces share the same shape: a small platform-aware helper
returns the right thing; the call site stays oblivious. Tests pin
both branches so we never regress one platform while fixing the other.
"""

from __future__ import annotations


class TestDetachedPopenKwargs:
    """`_detached_popen_kwargs(name)` returns the right Popen kwargs
    for spawning a backfill that outlives the request worker."""

    def test_posix_uses_start_new_session(self):
        from flowmetrics.app import _detached_popen_kwargs
        kw = _detached_popen_kwargs("posix")
        assert kw == {"start_new_session": True}

    def test_windows_uses_create_new_process_group(self):
        from flowmetrics.app import _detached_popen_kwargs
        kw = _detached_popen_kwargs("nt")
        # CREATE_NEW_PROCESS_GROUP = 0x00000200 — the Win32 flag that
        # decouples the child from the parent's console session.
        # Hard-coded so this test runs on any OS (the symbol is only
        # exposed on `subprocess` when running on Windows).
        assert kw == {"creationflags": 0x00000200}

    def test_unknown_os_falls_back_to_no_extra_kwargs(self):
        # We don't try to invent flags for hypothetical other OSes;
        # the call site is still safe (Popen without detach kwargs
        # is just a regular child).
        from flowmetrics.app import _detached_popen_kwargs
        assert _detached_popen_kwargs("emscripten") == {}


class TestPortBusyHints:
    """`_port_busy_hints(port, name)` returns the OS-appropriate
    'find/kill the holder' suggestion block used by the port-busy
    error message in `flow serve` and `flow samples serve`."""

    def test_posix_uses_lsof_and_kill(self):
        from flowmetrics.cli import _port_busy_hints
        text = _port_busy_hints(8000, "posix")
        assert "lsof -ti:8000" in text
        assert "kill $(lsof -ti:8000)" in text
        # No Windows commands leak in.
        assert "netstat" not in text
        assert "taskkill" not in text

    def test_windows_uses_netstat_and_taskkill(self):
        from flowmetrics.cli import _port_busy_hints
        text = _port_busy_hints(8000, "nt")
        assert "netstat -ano | findstr :8000" in text
        assert "taskkill /F /PID" in text
        # No POSIX commands leak in.
        assert "lsof" not in text
        assert "kill $(" not in text

    def test_alternate_port_suggestion_is_platform_agnostic(self):
        from flowmetrics.cli import _port_busy_hints
        posix = _port_busy_hints(8000, "posix")
        windows = _port_busy_hints(8000, "nt")
        # Both should suggest the same escape hatch.
        for hint in (posix, windows):
            assert "--port 8001" in hint
