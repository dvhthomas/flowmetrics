---
title: Run the dashboard locally
---

# Run the dashboard locally

> **Diátaxis: How-to.** Foreground `flow serve` — tied to your
> terminal, dies when the shell does. For a service that survives
> logout, see [Run as a persistent web server](run-as-persistent-server.md).

```bash
flow serve \
    --workflows-dir CONTRACTS_DIR \
    --data-dir       DATA_DIR
# → http://127.0.0.1:8000
```

- Reads only from the local Parquet warehouse. Never hits GitHub /
  Jira during a request.
- `--port N` picks an alternate port.
- `--host 0.0.0.0` binds publicly and **requires** `--password <…>`
  (or `$FLOW_PASSWORD`). HTTP Basic auth is then enforced.

## Stopping

`Ctrl-C` in the terminal. There is no daemon to manage in this mode.

## Next

- [Run as a persistent web server](run-as-persistent-server.md) — for
  a dashboard that survives logout, reboots, and crashes.
- [Troubleshooting § port already in use](troubleshooting.md#port-8000-on-127001-is-already-in-use)
