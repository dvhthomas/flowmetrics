"""`flow serve --bg` — Linux systemd-user implementation.

Hides systemctl mechanics behind two verbs that mirror the launchd
side (`install_and_start`, `stop_and_uninstall`). `render_serve_unit`
is the pure render function — extracted so the unit contract is
testable without systemd present.

User-level by default (`~/.config/systemd/user/`). No root, no
service manager outside the operator's session. The dashboard
survives a user logout only with `loginctl enable-linger $USER`;
the CLI prints that hint after install.

Why generate instead of shipping the templated unit at
`scripts/scheduling/linux-systemd/flowmetrics-serve.service`? A
`uv tool install` puts `flow` on PATH but doesn't ship the repo's
`scripts/` tree, so we have to render at install time anyway from
the resolved binary path + chosen flags. The templated file remains
the documentation artifact (and the advanced-user manual path).
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from . import BgError

# Canonical unit name. Matches the templated unit at
# scripts/scheduling/linux-systemd/ so a user switching between
# `--bg` and the manual path doesn't end up with two competing
# services under different names.
SERVE_UNIT = "flowmetrics-serve.service"


def render_serve_unit(
    *,
    flow_bin: Path,
    workflows_dir: Path,
    data_dir: Path,
    port: int,
    host: str,
    password: str | None,
    log_dir: Path,
) -> str:
    """Build the systemd `.service` unit text.

    All inputs must be absolute paths — systemd's parser doesn't
    expand `~` and the user unit doesn't inherit a CWD.

    Persistence: `Restart=on-failure` respawns the dashboard on
    crash. `WantedBy=default.target` lets `systemctl enable` make
    it auto-start at session boot. Login-independence requires
    `loginctl enable-linger $USER` — the CLI prints that hint.
    """
    exec_args = [
        str(flow_bin),
        "serve",
        f"--workflows-dir {workflows_dir}",
        f"--data-dir {data_dir}",
        f"--port {port}",
        f"--host {host}",
    ]
    if password is not None:
        exec_args.append(f"--password {password}")
    exec_line = " ".join(exec_args)

    return (
        "[Unit]\n"
        "Description=flowmetrics dashboard (persistent web server)\n"
        # Wait for the network so we can bind the listener. We
        # never hit external APIs from the request path, so
        # `network.target` (not `network-online.target`) is
        # enough.
        "After=network.target\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={data_dir.parent}\n"
        f"ExecStart={exec_line}\n"
        # systemd's `append:` mode keeps logs across restarts so
        # an operator can tail the file the same way the launchd
        # path documents (the OS unit logs already go to
        # `journalctl --user`, but file logs match the macOS
        # convention).
        f"StandardOutput=append:{log_dir}/serve.out.log\n"
        f"StandardError=append:{log_dir}/serve.err.log\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        # Bound startup so a stuck import doesn't wedge systemd.
        "TimeoutStartSec=30\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def default_user_unit_dir() -> Path:
    """`~/.config/systemd/user` — where `systemctl --user` looks for
    unit files. No root needed; the manager runs as the invoking
    user."""
    return Path.home() / ".config" / "systemd" / "user"


def _systemctl_user(*args: str) -> subprocess.CompletedProcess:
    """Wrap `systemctl --user <args>` so the call shape is one place.
    `check=False` everywhere — systemd's exit codes encode lots of
    "already in this state" situations we tolerate."""
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        capture_output=True,
    )


def install_and_start(
    *,
    unit_dir: Path,
    flow_bin: Path,
    workflows_dir: Path,
    data_dir: Path,
    port: int,
    host: str,
    password: str | None,
    log_dir: Path,
) -> Path:
    """Write the unit, daemon-reload, enable + restart. Returns the
    unit-file path. Idempotent — a second call against an already-
    running service reloads it with the latest flags via `restart`.

    The sequence is load-bearing:
      1. write the file
      2. daemon-reload  (systemd re-parses; otherwise enable picks
         up the OLD content)
      3. enable         (registers WantedBy=default.target)
      4. restart        (starts if not running, restarts if running,
                         so the latest ExecStart takes effect)
    """
    unit_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    unit_path = unit_dir / SERVE_UNIT
    unit_path.write_text(
        render_serve_unit(
            flow_bin=flow_bin,
            workflows_dir=workflows_dir,
            data_dir=data_dir,
            port=port,
            host=host,
            password=password,
            log_dir=log_dir,
        )
    )

    _systemctl_user("daemon-reload")
    enable = _systemctl_user("enable", SERVE_UNIT)
    if enable.returncode != 0:
        stderr = enable.stderr.decode("utf-8", errors="replace").strip()
        raise BgError(
            f"systemctl --user enable exited {enable.returncode}: "
            f"{stderr or '(no stderr)'}"
        )
    restart = _systemctl_user("restart", SERVE_UNIT)
    if restart.returncode != 0:
        stderr = restart.stderr.decode("utf-8", errors="replace").strip()
        raise BgError(
            f"systemctl --user restart exited {restart.returncode}: "
            f"{stderr or '(no stderr)'}"
        )

    return unit_path


def stop_and_uninstall(*, unit_dir: Path) -> None:
    """Disable + stop the service AND remove the unit file. Both
    steps are best-effort: missing unit + already-disabled service
    both round-trip to "nothing to do"."""
    # `disable --now` stops + disables in one call. Tolerate any
    # exit — the goal state is "service gone", and other failure
    # modes are downstream of that.
    _systemctl_user("disable", "--now", SERVE_UNIT)

    unit_path = unit_dir / SERVE_UNIT
    try:
        unit_path.unlink()
    except FileNotFoundError:
        pass

    # Re-read state so systemd forgets the now-removed unit.
    _systemctl_user("daemon-reload")
