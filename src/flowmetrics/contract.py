"""Workflow contract — durable config defining what to materialise.

A contract YAML declares which source to fetch from and which scope.
Slice 1 supports the minimum viable shape:

    contract:
      name: my-contract-name
      source: github          # or 'jira'
      repo: owner/name        # GitHub only
      jira_url: https://…     # Jira only
      jira_project: PROJ      # Jira only
      start: 2026-05-04       # window start (ISO date)
      stop: 2026-05-10        # window stop (ISO date)

Richer fields (stages, metadata extractors, exclusions) arrive in
later slices as the contract format grows toward the shape declared
in docs/SPEC-warehouse-app.md §5.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import yaml


class ContractError(ValueError):
    """Raised when a contract YAML is missing, malformed, or invalid."""


@dataclass(frozen=True)
class Contract:
    name: str
    source: Literal["github", "jira"]
    # GitHub fields
    repo: str | None = None
    # Jira fields
    jira_url: str | None = None
    jira_project: str | None = None
    # Window
    start: date | None = None
    stop: date | None = None


def load_contract(name: str, contracts_dir: Path) -> Contract:
    """Load and validate a contract YAML by name.

    Looks for `<name>.yaml` then `<name>.yml` under contracts_dir.
    Raises ContractError with a human-readable message on any
    failure — the caller (CLI) surfaces this to stderr and exits
    non-zero. Cron operators read the error in their mail.
    """
    path = contracts_dir / f"{name}.yaml"
    if not path.exists():
        alt = contracts_dir / f"{name}.yml"
        if alt.exists():
            path = alt
        else:
            raise ContractError(
                f"contract {name!r} not found under {contracts_dir}/ "
                f"(looked for {name}.yaml and {name}.yml)"
            )

    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ContractError(
            f"contract {name!r} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict) or "contract" not in raw:
        raise ContractError(
            f"contract file {path} must have a top-level `contract:` key"
        )

    body = raw["contract"]
    if not isinstance(body, dict):
        raise ContractError(
            f"`contract:` in {path} must be a mapping; "
            f"got {type(body).__name__}"
        )

    declared_name = body.get("name")
    if declared_name != name:
        raise ContractError(
            f"contract file is for name={declared_name!r} "
            f"but loaded as {name!r}"
        )

    source = body.get("source")
    if source not in ("github", "jira"):
        raise ContractError(
            f"contract.source must be 'github' or 'jira'; got {source!r}"
        )

    repo = body.get("repo")
    jira_url = body.get("jira_url")
    jira_project = body.get("jira_project")
    if source == "github" and not repo:
        raise ContractError("github contract requires `repo: OWNER/NAME`")
    if source == "jira" and not (jira_url and jira_project):
        raise ContractError(
            "jira contract requires `jira_url` and `jira_project`"
        )

    def _parse_date(field_name: str) -> date | None:
        v = body.get(field_name)
        if v is None:
            return None
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v))
        except (TypeError, ValueError) as exc:
            raise ContractError(
                f"contract.{field_name} must be YYYY-MM-DD; got {v!r}"
            ) from exc

    return Contract(
        name=name,
        source=source,
        repo=repo,
        jira_url=jira_url,
        jira_project=jira_project,
        start=_parse_date("start"),
        stop=_parse_date("stop"),
    )
