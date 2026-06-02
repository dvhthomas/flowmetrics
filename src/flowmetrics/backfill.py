"""Backfill status — a tiny per-workflow JSON file recording an
in-progress / finished `flow materialize` run.

A browser-triggered backfill (the Data Source page) spawns
`flow materialize` as a detached subprocess; that process writes
this file (running → done/failed) and the page polls it. The
file doubles as a lock: one backfill per workflow at a time.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

_STATUS_DIR = "_status"

# A `running` record older than this is treated as dead — the
# subprocess crashed (OOM, reboot, kill) without writing a
# terminal record. Past this, a new backfill is allowed so the
# status-file lock can't wedge a workflow forever.
STALE_AFTER = timedelta(minutes=10)


def status_path(data_dir: Path, workflow: str) -> Path:
    """The backfill status file for `workflow` under `data_dir`."""
    return Path(data_dir) / _STATUS_DIR / f"{workflow}.json"


def write_status(path: Path, data: dict) -> None:
    """Atomically write `data` as JSON to `path` — tmp write then
    rename, so a concurrent poll never sees a half-written file.
    Creates the parent directory if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f"{path.name}.tmp"
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def read_status(path: Path) -> dict | None:
    """Read the JSON status file, or None when it is absent or
    unreadable (a torn read is treated as "no status yet")."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def is_active(status: dict | None, now: datetime) -> bool:
    """True when `status` is a backfill still legitimately running
    — status is "running" AND it started within `STALE_AFTER` of
    `now`. A stale "running" record (the subprocess crashed
    without writing done/failed) returns False, so the lock can't
    wedge a workflow permanently."""
    if not status or status.get("status") != "running":
        return False
    started = status.get("started_at")
    if not started:
        return False
    try:
        started_dt = datetime.fromisoformat(str(started))
    except ValueError:
        return False
    return now - started_dt < STALE_AFTER


def display_status(status: dict | None, now: datetime) -> dict | None:
    """The status to show in the UI. A stale "running" record is
    surfaced as "failed" so the progress fragment stops polling
    instead of spinning forever on a crashed subprocess."""
    if (
        status
        and status.get("status") == "running"
        and not is_active(status, now)
    ):
        return {
            **status,
            "status": "failed",
            "message": (
                "Backfill stopped responding — it may have "
                "crashed. Try again."
            ),
        }
    return status
