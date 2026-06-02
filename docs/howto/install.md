---
title: Install on macOS, Linux, Windows
---

# Install on macOS, Linux, Windows

> **Diátaxis: How-to.** Single recipe for getting `flow` onto a fresh
> machine.

Same path on every OS: install `uv`, then `uv tool install`.

```bash
# macOS
brew install uv

# Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows (PowerShell)
irm https://astral.sh/uv/install.ps1 | iex
```

```bash
# Install flowmetrics as a global tool (isolated env, exposes `flow`).
uv tool install git+https://github.com/dvhthomas/flowmetrics

# Verify.
flow --help
which flow                          # macOS/Linux
where flow                          # Windows
```

`flow` lands in `~/.local/bin/` on macOS/Linux and
`%USERPROFILE%\.local\bin\` on Windows. If those aren't on your PATH,
`uv tool install` prints the line to add.

## Credentials

- **GitHub** — `gh auth login` (uses `gh auth token`) or set
  `$GITHUB_TOKEN` directly. A fine-grained PAT with **public repo
  read** is enough for public repos.
- **Atlassian Jira** — public instances (e.g. Apache's
  `https://issues.apache.org/jira`) need no credentials. Private
  instances: see [Reference § Workflow YAML](../reference.md#workflow-yaml).

## Next

- [Add a workflow in the browser](add-workflow-in-browser.md)
- [Write a workflow YAML by hand](write-workflow-yaml.md)
