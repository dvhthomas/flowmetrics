"""`flow serve --bg` — install + start the dashboard as a native
persistent service.

This package is the platform-blind entry point. Callers (the CLI)
import `install_and_start` / `stop_and_uninstall` / `BgError` from
here and don't need to know whether the host runs launchd, systemd,
or something else.

Internals split by host:

  - `bg.launchd`  — macOS LaunchAgent under `~/Library/LaunchAgents/`.
  - `bg.systemd`  — Linux user unit under `~/.config/systemd/user/`.

Unsupported platforms (Windows, BSD) raise `BgError` with a pointer
at the templated service files under `scripts/scheduling/`.
"""
from __future__ import annotations

import sys
from pathlib import Path


class BgError(Exception):
    """Raised when --bg can't proceed (unsupported platform, missing
    `launchctl` / `systemctl`, malformed paths, etc.). The CLI
    surfaces .args[0] as the user-facing message."""


def install_and_start(
    *,
    flow_bin: Path,
    workflows_dir: Path,
    data_dir: Path,
    port: int,
    host: str,
    password: str | None,
    log_dir: Path,
) -> Path:
    """Install + start `flow serve` as a persistent native service
    for the current user. Idempotent — a second call reloads with
    the latest flags.

    Returns the path to the on-disk plist/unit file so the CLI can
    surface where to look for trouble.

    Raises `BgError` on unsupported platforms or shell-out failures.
    """
    if sys.platform == "darwin":
        from . import launchd
        return launchd.install_and_start(
            launchagents_dir=launchd.default_launchagents_dir(),
            uid=launchd.current_uid(),
            flow_bin=flow_bin,
            workflows_dir=workflows_dir,
            data_dir=data_dir,
            port=port,
            host=host,
            password=password,
            log_dir=log_dir,
        )
    if sys.platform.startswith("linux"):
        from . import systemd
        return systemd.install_and_start(
            unit_dir=systemd.default_user_unit_dir(),
            flow_bin=flow_bin,
            workflows_dir=workflows_dir,
            data_dir=data_dir,
            port=port,
            host=host,
            password=password,
            log_dir=log_dir,
        )
    raise BgError(
        "`flow serve --bg` supports macOS (launchd) and Linux "
        "(systemd --user) in this release. Other platforms: use "
        "the templated service files under scripts/scheduling/. "
        "See docs/HOWTO.md#run-as-a-persistent-web-server."
    )


def stop_and_uninstall() -> None:
    """Tear down the persistent service. Idempotent — calling
    twice is fine."""
    if sys.platform == "darwin":
        from . import launchd
        launchd.stop_and_uninstall(
            launchagents_dir=launchd.default_launchagents_dir(),
            uid=launchd.current_uid(),
        )
        return
    if sys.platform.startswith("linux"):
        from . import systemd
        systemd.stop_and_uninstall(
            unit_dir=systemd.default_user_unit_dir(),
        )
        return
    raise BgError(
        "`flow serve --bg --stop` supports macOS + Linux only. "
        "Other platforms: use the templated service files under "
        "scripts/scheduling/."
    )


def install_materialize_schedule(
    *,
    flow_bin: Path,
    materialize_args: list[str],
    hour: int,
    minute: int,
    log_dir: Path,
) -> Path:
    """Install + activate a scheduled `flow materialize` job for the
    current user. Idempotent — re-running reloads with the latest
    flags / schedule.

    `materialize_args` is everything that should follow `flow
    materialize` on the command line (typically `["--all",
    "--workflows-dir", PATH, "--data-dir", PATH]`).

    Returns the path to the on-disk plist/unit file.
    """
    if sys.platform == "darwin":
        from . import launchd
        return launchd.install_materialize_schedule(
            launchagents_dir=launchd.default_launchagents_dir(),
            uid=launchd.current_uid(),
            flow_bin=flow_bin,
            materialize_args=materialize_args,
            hour=hour, minute=minute,
            log_dir=log_dir,
        )
    if sys.platform.startswith("linux"):
        raise BgError(
            "`flow materialize --bg` ships with macOS launchd "
            "support today. Linux systemd-timer support is on "
            "the roadmap — meanwhile, the templated unit + timer "
            "under scripts/scheduling/linux-systemd/ handle "
            "scheduling on Linux."
        )
    raise BgError(
        "`flow materialize --bg` supports macOS (launchd) in this "
        "release. Other platforms: use the templated scheduler "
        "files under scripts/scheduling/."
    )


def stop_materialize_schedule() -> None:
    """Tear down the scheduled materialize job. Idempotent."""
    if sys.platform == "darwin":
        from . import launchd
        launchd.stop_materialize_schedule(
            launchagents_dir=launchd.default_launchagents_dir(),
            uid=launchd.current_uid(),
        )
        return
    if sys.platform.startswith("linux"):
        raise BgError(
            "`flow materialize --bg --stop` ships with macOS "
            "support today. Linux: remove the templated unit + "
            "timer files yourself."
        )
    raise BgError(
        "`flow materialize --bg --stop` supports macOS only."
    )


__all__ = [
    "BgError",
    "install_and_start",
    "install_materialize_schedule",
    "stop_and_uninstall",
    "stop_materialize_schedule",
]
