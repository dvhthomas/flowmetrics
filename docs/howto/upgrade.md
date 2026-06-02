---
title: Upgrade
---

# Upgrade

> **Diátaxis: How-to.** Pull a newer `flow` build onto an existing
> install.

```bash
# Re-fetch latest main and reinstall.
uv tool upgrade flowmetrics

# Confirm the new version landed.
flow --version
# → flow 0.1.0                   (tagged release)
# → flow 0.1.0.dev3+ge8a2cd1     (intermediate commit)

# Source checkout?
git pull && uv sync --reinstall-package flowmetrics
```

Versions are derived from git via `hatch-vcs`: tagged commits render
as plain PEP-440 (`0.1.0`), intermediate commits as
`0.1.0.devN+g<sha>` where `N` counts commits since the last tag and
`<sha>` is the short commit hash. The CI build, your local checkout,
and a `uv tool install` all converge on the same version for the
same git state.

## After upgrade

If a running `flow serve --bg` is on an old build, re-run
`flow serve --bg` (idempotent) to reload the LaunchAgent / systemd
unit against the upgraded binary.

If Parquet read errors crop up (a major DuckDB bump can change
on-disk shape), restore from the most recent good backup into a
fresh `--data-dir` or re-run `flow materialize --all` — the
warehouse is downstream of the source API, never the source of
truth. See [Back up and restore](backup-and-restore.md) and
[Troubleshooting § Parquet read errors](troubleshooting.md#parquet-read-errors-after-upgrade).
