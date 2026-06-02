"""`flow serve --bg` — macOS launchd implementation.

Hides launchctl mechanics behind two verbs:

  - `install_and_start(…)`  → idempotent install (writes the plist,
                              bootouts any existing instance, then
                              bootstraps). Restart-if-running.
  - `stop_and_uninstall(…)` → bootout + remove the plist. Idempotent.

`render_serve_plist(…)` is a pure function returning the plist XML —
extracted so we can unit-test the workflow with launchd without
needing launchd to run.

Why not just shell out to the templated `com.flowmetrics.serve.plist`
under scripts/scheduling/? Because a `uv tool install` puts `flow` on
PATH but doesn't ship the repo's `scripts/` tree. The plist has to
be generated at install time from the user's actual `flow` binary
path and chosen flags. The templated file remains the documentation
artifact (and the advanced-user manual path).
"""
from __future__ import annotations

import contextlib
import os
import plistlib
import subprocess
from pathlib import Path

from . import BgError

# Canonical agent labels. Mirror the templated plists so a user
# switching between `--bg` and the manual install path doesn't end
# up with two competing agents under different labels.
SERVE_LABEL = "com.flowmetrics.serve"
MATERIALIZE_LABEL = "com.flowmetrics.materialize"

# launchctl exit code when bootout is called on an agent that isn't
# loaded. Not an error — just nothing to undo.
_BOOTOUT_NOT_LOADED = 113


def render_serve_plist(
    *,
    label: str,
    flow_bin: Path,
    workflows_dir: Path,
    data_dir: Path,
    port: int,
    host: str,
    password: str | None,
    log_dir: Path,
) -> bytes:
    """Build the launchd plist XML for a persistent `flow serve`.

    All inputs MUST be absolute paths. launchd doesn't inherit a
    CWD; relative paths inside a plist resolve against `/`.

    Persistence is encoded via `RunAtLoad=true` + `KeepAlive=true`:
    launchd starts the agent at user login AND respawns it on
    crash/clean-exit. Same shape as the templated manual install
    under scripts/scheduling/macos-launchd/.
    """
    args: list[str] = [
        str(flow_bin),
        "serve",
        "--workflows-dir", str(workflows_dir),
        "--data-dir", str(data_dir),
        "--port", str(port),
        "--host", host,
    ]
    if password is not None:
        args.extend(["--password", password])

    spec: dict[str, object] = {
        "Label": label,
        "ProgramArguments": args,
        # Working dir lets relative paths inside flow serve (the
        # default cache dir, the `data/` default) resolve under the
        # operator's install root.
        "WorkingDirectory": str(data_dir.parent),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(log_dir / "serve.out.log"),
        "StandardErrorPath": str(log_dir / "serve.err.log"),
        # Inherit a workable PATH so /opt/homebrew/bin etc. resolve
        # — launchd starts agents with a near-empty PATH otherwise.
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    }
    return plistlib.dumps(spec)


def install_and_start(
    *,
    launchagents_dir: Path,
    flow_bin: Path,
    workflows_dir: Path,
    data_dir: Path,
    port: int,
    host: str,
    password: str | None,
    log_dir: Path,
    uid: int,
) -> Path:
    """Write the plist, (re-)bootstrap the agent, return the plist
    path. Idempotent — a second call against a running agent
    reloads it with the current flags.

    `launchagents_dir`, `log_dir`, and the path arguments should be
    absolute; the CLI resolves them before calling.

    Platform routing is the dispatcher's job (`flowmetrics.bg`); this
    function will happily run anywhere that has `launchctl` on PATH,
    which makes it cleanly testable with mocked subprocess.
    """

    launchagents_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    plist_path = launchagents_dir / f"{SERVE_LABEL}.plist"
    plist_path.write_bytes(
        render_serve_plist(
            label=SERVE_LABEL,
            flow_bin=flow_bin,
            workflows_dir=workflows_dir,
            data_dir=data_dir,
            port=port,
            host=host,
            password=password,
            log_dir=log_dir,
        )
    )

    domain = f"gui/{uid}"
    target = f"{domain}/{SERVE_LABEL}"

    # bootout first — tolerate "not loaded" exit (113). Without
    # this, a second `--bg` invocation against a running agent
    # would error out instead of reloading.
    subprocess.run(
        ["launchctl", "bootout", target],
        check=False,
        capture_output=True,
    )
    # bootstrap — install + start. Failure here is a real error
    # (e.g. malformed plist, permissions issue); surface it.
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = (
            result.stderr.decode("utf-8", errors="replace")
            if isinstance(result.stderr, bytes)
            else (result.stderr or "")
        )
        raise BgError(
            f"launchctl bootstrap exited {result.returncode}: "
            f"{stderr.strip() or '(no stderr)'}"
        )

    return plist_path


def stop_and_uninstall(*, launchagents_dir: Path, uid: int) -> None:
    """Bootout the agent and remove its plist. Both steps are
    best-effort: missing plist + unloaded agent both round-trip to
    "nothing to do"."""
    target = f"gui/{uid}/{SERVE_LABEL}"
    subprocess.run(
        ["launchctl", "bootout", target],
        check=False,
        capture_output=True,
    )

    plist_path = launchagents_dir / f"{SERVE_LABEL}.plist"
    with contextlib.suppress(FileNotFoundError):
        plist_path.unlink()


def render_materialize_plist(
    *,
    label: str,
    flow_bin: Path,
    materialize_args: list[str],
    hour: int,
    minute: int,
    log_dir: Path,
) -> bytes:
    """Build the launchd plist XML for a scheduled `flow materialize`.

    Unlike `render_serve_plist` (RunAtLoad + KeepAlive — a
    long-running daemon), this is a one-shot scheduled job: launchd
    fires it once per matching calendar interval, lets it exit, and
    waits for the next match.

    `materialize_args` is everything that should follow `flow
    materialize` on the command line — typically `["--all",
    "--workflows-dir", PATH, "--data-dir", PATH]` for a daily-cron
    install, or `[NAME, "--workflows-dir", PATH, "--data-dir",
    PATH]` for a single-workflow schedule.

    The hour/minute are LOCAL time. The Mac's timezone decides what
    that maps to in UTC.
    """
    args: list[str] = [str(flow_bin), "materialize", *materialize_args]
    spec: dict[str, object] = {
        "Label": label,
        "ProgramArguments": args,
        "StartCalendarInterval": {"Hour": int(hour), "Minute": int(minute)},
        # Run on next wake if the Mac was asleep when the calendar
        # interval fired — without this, sleeping through 6 AM means
        # skipping the day entirely.
        "StartCalendarIntervalDoesNotFireWhenSleeping": False,
        "StandardOutPath": str(log_dir / "materialize.out.log"),
        "StandardErrorPath": str(log_dir / "materialize.err.log"),
        # Inherit a workable PATH so /opt/homebrew/bin etc. resolve.
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin",
        },
    }
    return plistlib.dumps(spec)


def install_materialize_schedule(
    *,
    launchagents_dir: Path,
    flow_bin: Path,
    materialize_args: list[str],
    hour: int,
    minute: int,
    log_dir: Path,
    uid: int,
) -> Path:
    """Write the materialize plist + (re-)bootstrap. Idempotent —
    safe to re-run; reloads the schedule with the latest flags."""
    launchagents_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    plist_path = launchagents_dir / f"{MATERIALIZE_LABEL}.plist"
    plist_path.write_bytes(
        render_materialize_plist(
            label=MATERIALIZE_LABEL,
            flow_bin=flow_bin,
            materialize_args=materialize_args,
            hour=hour, minute=minute,
            log_dir=log_dir,
        )
    )

    domain = f"gui/{uid}"
    target = f"{domain}/{MATERIALIZE_LABEL}"
    subprocess.run(
        ["launchctl", "bootout", target],
        check=False, capture_output=True,
    )
    result = subprocess.run(
        ["launchctl", "bootstrap", domain, str(plist_path)],
        check=False, capture_output=True,
    )
    if result.returncode != 0:
        stderr = (
            result.stderr.decode("utf-8", errors="replace")
            if isinstance(result.stderr, bytes) else (result.stderr or "")
        )
        raise BgError(
            f"launchctl bootstrap exited {result.returncode}: "
            f"{stderr.strip() or '(no stderr)'}"
        )
    return plist_path


def stop_materialize_schedule(
    *, launchagents_dir: Path, uid: int,
) -> None:
    """Bootout the schedule + remove the plist. Idempotent."""
    target = f"gui/{uid}/{MATERIALIZE_LABEL}"
    subprocess.run(
        ["launchctl", "bootout", target],
        check=False, capture_output=True,
    )
    plist_path = launchagents_dir / f"{MATERIALIZE_LABEL}.plist"
    with contextlib.suppress(FileNotFoundError):
        plist_path.unlink()


def default_launchagents_dir() -> Path:
    """`~/Library/LaunchAgents` — the per-user LaunchAgents
    directory. No root needed; the agent runs as the invoking user."""
    return Path.home() / "Library" / "LaunchAgents"


def current_uid() -> int:
    """Effective UID — what launchctl uses to form the `gui/$UID`
    domain. Wrapped so tests can fake it without `monkeypatch`-ing
    os globally."""
    return os.getuid()
