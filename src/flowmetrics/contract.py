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
class WorkflowStates:
    """3-category classification of workflow states. Order
    within each tuple is the kanban order (left to right).

    - `backlog`: items not yet started; excluded from CFD and
      Aging WIP entirely (Vacanti — backlog is not WIP).
    - `wip`: actively-being-worked states; each becomes a band
      on the CFD and a column on Aging WIP.
    - `done`: terminal/departure states; CFD shows them as the
      bottom band(s). Aging excludes them (items have completed).
    """

    backlog: tuple[str, ...] = ()
    wip: tuple[str, ...] = ()
    done: tuple[str, ...] = ()

    def cfd_bands(self) -> tuple[str, ...]:
        """States to render as CFD bands, in kanban order:
        WIP first (top of stack), done at the bottom
        (departures). Backlog is excluded."""
        return self.wip + self.done


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
    # Optional 3-category state classification. When set,
    # stage-aware views (CFD, Aging) consult this to filter and
    # order. When unset, views fall back to data-derived ordering.
    states: WorkflowStates | None = None
    # Optional human-friendly display name. The UI shows this in
    # breadcrumbs / the home-page workflow list. Falls back to
    # `name` when omitted. `name` stays the routing ID
    # (unique, URL-safe) — `label` is just prose.
    label: str | None = None


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

    return parse_contract_text(path.read_text(), name)


def parse_contract_text(text: str, name: str) -> Contract:
    """Same validation as `load_contract`, but takes raw YAML text
    instead of a file path. The write API (PUT
    /api/internal/contracts/{id}) routes the body through this so
    validation logic is never duplicated."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractError(
            f"contract {name!r} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict) or "contract" not in raw:
        raise ContractError(
            "must have a top-level `contract:` key"
        )

    body = raw["contract"]
    if not isinstance(body, dict):
        raise ContractError(
            f"`contract:` must be a mapping; got {type(body).__name__}"
        )

    declared_name = body.get("name")
    if declared_name != name:
        raise ContractError(
            f"contract file is for name={declared_name!r} "
            f"but loaded as {name!r}"
        )

    raw_label = body.get("label")
    if raw_label is not None and not isinstance(raw_label, str):
        raise ContractError(
            f"contract.label must be a string; got "
            f"{type(raw_label).__name__}"
        )
    label = raw_label

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

    # Optional `states:` block — 3-category classification of
    # workflow states (backlog/wip/done). Within each category the
    # list order is the kanban order (no inference). Each state
    # name can appear in at most one category. Reclassify by
    # moving a state name between lists.
    raw_states = body.get("states")
    states_obj: WorkflowStates | None
    if raw_states is None:
        states_obj = None
    else:
        if not isinstance(raw_states, dict):
            raise ContractError(
                f"contract.states must be a mapping of "
                f"category → [state_name, …]; got "
                f"{type(raw_states).__name__}"
            )
        valid_categories = {"backlog", "wip", "done"}
        seen: dict[str, str] = {}  # state_name → category
        parsed: dict[str, tuple[str, ...]] = {
            "backlog": (), "wip": (), "done": (),
        }
        for category, names in raw_states.items():
            if category not in valid_categories:
                raise ContractError(
                    f"unknown category {category!r} in contract.states; "
                    f"valid categories are: backlog, wip, done"
                )
            if not isinstance(names, list):
                raise ContractError(
                    f"contract.states.{category} must be a list of "
                    f"state names; got {type(names).__name__}"
                )
            names_tuple = tuple(str(n) for n in names)
            for n in names_tuple:
                if n in seen:
                    raise ContractError(
                        f"state {n!r} appears in more than one "
                        f"category: {seen[n]!r} and {category!r}. "
                        f"Each state name can be in at most one "
                        f"category."
                    )
                seen[n] = category
            parsed[category] = names_tuple
        states_obj = WorkflowStates(**parsed)

    return Contract(
        name=name,
        source=source,
        repo=repo,
        jira_url=jira_url,
        jira_project=jira_project,
        start=_parse_date("start"),
        stop=_parse_date("stop"),
        states=states_obj,
        label=label,
    )


def validate_yaml_text_structured(
    text: str, name: str | None = None,
) -> list[dict]:
    """Structured-error variant for the validate-on-keystroke API.
    Returns `[]` when the text is a valid contract; otherwise a list
    of `{message, line, column}` dicts (line/column null for semantic
    errors that don't pin a single row).

    `name` lets the caller assert the parsed contract.name matches a
    URL slug; pass None to skip that check."""
    try:
        if name is None:
            # Parse without the slug check by extracting it from the
            # body and feeding it back in. If the body doesn't even
            # have a `name`, parse_contract_text will surface that.
            try:
                doc = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                return [_yaml_err_to_struct(exc)]
            if not isinstance(doc, dict) or not isinstance(doc.get("contract"), dict):
                return [{
                    "message": "must have a top-level `contract:` key",
                    "line": None, "column": None,
                }]
            inferred = doc["contract"].get("name") or ""
            parse_contract_text(text, inferred)
        else:
            parse_contract_text(text, name)
    except ContractError as exc:
        # ContractError messages may wrap a YAMLError; preserve
        # location info when present.
        cause = exc.__cause__
        if isinstance(cause, yaml.YAMLError):
            return [_yaml_err_to_struct(cause)]
        return [{"message": str(exc), "line": None, "column": None}]
    except yaml.YAMLError as exc:
        return [_yaml_err_to_struct(exc)]
    return []


def _yaml_err_to_struct(exc: yaml.YAMLError) -> dict:
    """PyYAML's YAMLError carries a `problem_mark` with line/column
    on syntax errors. Pull that out so the UI can underline the
    bad line."""
    line = column = None
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
        line = int(getattr(mark, "line", -1)) + 1   # 0-indexed → 1-indexed
        column = int(getattr(mark, "column", -1)) + 1
    msg = getattr(exc, "problem", None) or str(exc)
    return {"message": msg, "line": line, "column": column}
