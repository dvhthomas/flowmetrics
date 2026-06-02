---
title: Develop against a source checkout
---

# Develop against a source checkout

> **Diátaxis: How-to.** Iterate on the codebase with the global
> `flow` command pointed at your tree.

```bash
git clone https://github.com/dvhthomas/flowmetrics
cd flowmetrics
uv sync                            # creates .venv/

# Run from the checkout.
uv run flow --help
uv run pytest                      # unit suite (no network)
uv run pytest -m integration       # opt-in, needs gh auth
uv run ruff check
uv run ty check src
```

## Editable install — global `flow` follows your edits

```bash
uv tool install --force --editable .
```

`--editable` keeps the global `flow` pointed at your source tree so
edits take effect without a re-install.
