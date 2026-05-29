"""Contract: canonical workflow definition.

A `Contract` describes one workflow's identity (id, label), source
(GitHub or Jira target), window, and the **ordered list of steps**
items move through. Each `Step` carries a name and a `wip: bool` flag.

The on-disk / over-the-wire shape is YAML. The canonical model is a
Pydantic `BaseModel` — writes go through Pydantic validation,
exports go through `emit_canonical_yaml`. The store (SQLite in C1+)
keeps each contract as a YAML text body so adding fields to the
Pydantic model never requires a DB migration.

Legacy `states: {backlog, wip, done}` YAMLs are still accepted on
import; `parse_contract_text` normalises them into the new
`steps: [{name, wip}]` shape. A `Contract.states` compatibility
property synthesises the legacy `WorkflowStates(backlog, wip, done)`
object so every existing CFD / Aging / charts caller keeps working
without modification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from . import signals


class ContractError(ValueError):
    """Raised when a contract YAML is missing, malformed, or invalid."""


MatcherKind = Literal["event", "label", "status", "stage"]


class Matcher(BaseModel):
    """One typed condition a step matches on.

      - `event`  → a lifecycle event; `value` is a source-scoped code
        (`pr-merged`, `status-changed`, …) → compared to a transition's
        `signal`.
      - `label` / `status` / `stage` → `value` is compared to a
        transition's `stage` text (a GitHub label, a Jira status, or a
        raw adapter stage).

    YAML shape is the single-key mapping `{event: pr-merged}`; the
    before-validator normalises it into `{kind, value}`. Bare strings are
    rejected — a matcher must declare its kind.
    """

    model_config = ConfigDict(frozen=True)

    kind: MatcherKind
    value: str

    @model_validator(mode="before")
    @classmethod
    def _from_single_key_mapping(cls, data: object) -> object:
        # Already {kind, value} (e.g. constructed directly) → pass through.
        if isinstance(data, dict) and "kind" in data and "value" in data:
            return data
        if isinstance(data, dict) and len(data) == 1:
            (k, v), = data.items()
            if k in ("event", "label", "status", "stage"):
                return {"kind": k, "value": str(v)}
            raise ValueError(
                f"unknown matcher kind {k!r}; use one of "
                "event / label / status / stage"
            )
        raise ValueError(
            "a matcher must be a typed mapping like {event: pr-merged}; "
            f"got {data!r}"
        )


# ---------------------------------------------------------------------------
# Legacy compatibility — `WorkflowStates` stays importable + constructable
# because a lot of existing tests + chart components consume it directly.
# It now also doubles as the synthesised return shape of
# `Contract.states`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowStates:
    """3-category classification of workflow states. Order
    within each tuple is the kanban order (left to right).

    - `backlog`: items not yet started; excluded from CFD and
      Aging WIP entirely (backlog is not WIP).
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


# ---------------------------------------------------------------------------
# Canonical Pydantic model.
# ---------------------------------------------------------------------------


class Step(BaseModel):
    """One workflow step: a user-defined logical bucket that
    materialised source data lands in.

    Fields:
      - `name`: the user's display name ("Ready", "In Review", …).
      - `wip`: does this step count as Work-In-Progress? Drives the
        CFD bands and the Aging WIP columns.
      - `matches`: the typed conditions (labels, statuses, lifecycle
        events) whose data fills this step. Empty list → the step's
        `name` is treated as a raw `stage` to match.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    wip: bool = False
    matches: list[Matcher] = []

    @field_validator("name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("step name must be a non-empty string")
        return v

    @property
    def effective_matchers(self) -> tuple[Matcher, ...]:
        """What this step captures: `matches` if non-empty, else a
        single `stage` matcher on the step's own name."""
        if self.matches:
            return tuple(self.matches)
        return (Matcher(kind="stage", value=self.name),)


class Contract(BaseModel):
    """One workflow's canonical definition."""

    model_config = ConfigDict(frozen=True)

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
    # Optional human-friendly display name. Falls back to `name`.
    label: str | None = None
    # Ordered list of workflow steps. Empty list = no states block;
    # CFD / Aging fall back to data-derived ordering.
    steps: list[Step] = []

    @model_validator(mode="after")
    def _validate_event_codes(self) -> Contract:
        """An `event:` matcher must use a code valid for this source."""
        valid = signals.event_codes_for(self.source)
        for step in self.steps:
            for m in step.matches:
                if m.kind == "event" and m.value not in valid:
                    raise ValueError(
                        f"step {step.name!r}: unknown {self.source} event "
                        f"code {m.value!r}; valid codes: "
                        f"{', '.join(sorted(valid))}"
                    )
        return self

    @property
    def states(self) -> WorkflowStates | None:
        """Compatibility shim. Synthesises the legacy 3-category
        WorkflowStates from `steps`:
          - leading non-WIP rows → backlog
          - contiguous WIP rows → wip
          - trailing non-WIP rows → done
        Returns None when the contract has no steps at all
        (matches the legacy "no states: block" behaviour)."""
        if not self.steps:
            return None
        backlog: list[str] = []
        wip: list[str] = []
        done: list[str] = []
        # Walk forward; the first WIP row ends backlog. Once any
        # WIP has been seen, every subsequent non-WIP belongs to
        # done. Intermixed WIP / non-WIP is collapsed by this rule
        # (the UI's category badges enforce contiguity at edit time).
        seen_wip = False
        for s in self.steps:
            if s.wip:
                wip.append(s.name)
                seen_wip = True
            else:
                if seen_wip:
                    done.append(s.name)
                else:
                    backlog.append(s.name)
        return WorkflowStates(
            backlog=tuple(backlog), wip=tuple(wip), done=tuple(done),
        )


# ---------------------------------------------------------------------------
# YAML import / export.
# ---------------------------------------------------------------------------


def parse_contract_text(text: str, name: str) -> Contract:
    """Parse a YAML string into a Contract. Accepts both the new
    `steps:` shape and the legacy `states: {backlog, wip, done}`
    shape; both produce the same canonical Contract object."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ContractError(
            f"contract {name!r} is not valid YAML: {exc}"
        ) from exc

    if not isinstance(raw, dict) or "contract" not in raw:
        raise ContractError("must have a top-level `contract:` key")

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

    label = body.get("label")
    if label is not None and not isinstance(label, str):
        raise ContractError(
            f"contract.label must be a string; got {type(label).__name__}"
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

    steps = _read_steps(body)

    try:
        return Contract(
            name=name,
            source=source,
            repo=repo,
            jira_url=jira_url,
            jira_project=jira_project,
            start=_parse_date("start"),
            stop=_parse_date("stop"),
            label=label,
            steps=steps,
        )
    except ValidationError as exc:
        raise ContractError(_summarise_pydantic_errors(exc)) from exc


def _read_steps(body: dict) -> list[Step]:
    """Read either the new `steps:` shape OR the legacy
    `states: {backlog, wip, done}` shape into a canonical Step list."""
    new_shape = body.get("steps")
    legacy_shape = body.get("states")
    if new_shape is not None and legacy_shape is not None:
        raise ContractError(
            "contract may carry `steps:` or `states:` but not both"
        )
    if new_shape is not None:
        return _read_new_steps(new_shape)
    if legacy_shape is not None:
        return _read_legacy_states(legacy_shape)
    return []


def _read_new_steps(raw: object) -> list[Step]:
    if not isinstance(raw, list):
        raise ContractError(
            f"contract.steps must be a list; got {type(raw).__name__}"
        )
    out: list[Step] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise ContractError(
                f"contract.steps[{i}] must be a mapping; "
                f"got {type(row).__name__}"
            )
        try:
            out.append(Step(**row))
        except ValidationError as exc:
            raise ContractError(
                f"contract.steps[{i}]: {_summarise_pydantic_errors(exc)}"
            ) from exc
    return out


def _read_legacy_states(raw: object) -> list[Step]:
    """Convert the legacy 3-category mapping into an ordered Step
    list. Order: backlog rows (wip=False), then wip rows (wip=True),
    then done rows (wip=False)."""
    if not isinstance(raw, dict):
        raise ContractError(
            f"contract.states must be a mapping of "
            f"category → [state_name, …]; got {type(raw).__name__}"
        )
    valid = {"backlog", "wip", "done"}
    seen: dict[str, str] = {}
    buckets: dict[str, list[str]] = {"backlog": [], "wip": [], "done": []}
    for category, names in raw.items():
        if category not in valid:
            raise ContractError(
                f"unknown category {category!r} in contract.states; "
                f"valid categories are: backlog, wip, done"
            )
        if not isinstance(names, list):
            raise ContractError(
                f"contract.states.{category} must be a list of "
                f"state names; got {type(names).__name__}"
            )
        for n in names:
            name = str(n)
            if name in seen:
                raise ContractError(
                    f"state {name!r} appears in more than one "
                    f"category: {seen[name]!r} and {category!r}. "
                    f"Each state name can be in at most one "
                    f"category."
                )
            seen[name] = category
            buckets[category].append(name)
    out: list[Step] = []
    for name in buckets["backlog"]:
        out.append(Step(name=name, wip=False))
    for name in buckets["wip"]:
        out.append(Step(name=name, wip=True))
    for name in buckets["done"]:
        out.append(Step(name=name, wip=False))
    return out


def _summarise_pydantic_errors(exc: ValidationError) -> str:
    parts: list[str] = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if p != "__root__")
        msg = err.get("msg", "invalid value")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts) or str(exc)


def load_contract(name: str, contracts_dir: Path) -> Contract:
    """Load and validate a contract YAML by name from
    `contracts_dir`.

    Looks for `<name>.yaml` then `<name>.yml`. Raises `ContractError`
    on any failure. Kept as a thin helper for tests + the CLI's
    --from-yaml path; the runtime server reads from the SQLite
    store via `flowmetrics.contracts_db.ContractsDB.get` instead.
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


def emit_canonical_yaml(contract: Contract) -> str:
    """Render a Contract in the canonical `steps:` YAML shape.

    The output is what `parse_contract_text` would round-trip
    against; what the export-YAML endpoint serves; what the SQLite
    store keeps in its `yaml` column."""
    body: dict = {"name": contract.name, "source": contract.source}
    if contract.label is not None:
        body["label"] = contract.label
    if contract.repo is not None:
        body["repo"] = contract.repo
    if contract.jira_url is not None:
        body["jira_url"] = contract.jira_url
    if contract.jira_project is not None:
        body["jira_project"] = contract.jira_project
    if contract.start is not None:
        body["start"] = contract.start.isoformat()
    if contract.stop is not None:
        body["stop"] = contract.stop.isoformat()
    if contract.steps:
        body["steps"] = []
        for s in contract.steps:
            row: dict = {"name": s.name, "wip": s.wip}
            if s.matches:
                row["matches"] = [{m.kind: m.value} for m in s.matches]
            body["steps"].append(row)
    return yaml.safe_dump(
        {"contract": body},
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
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
            try:
                doc = yaml.safe_load(text)
            except yaml.YAMLError as exc:
                return [_yaml_err_to_struct(exc)]
            if not isinstance(doc, dict) or not isinstance(
                doc.get("contract"), dict
            ):
                return [{
                    "message": "must have a top-level `contract:` key",
                    "line": None, "column": None,
                }]
            inferred = doc["contract"].get("name") or ""
            parse_contract_text(text, inferred)
        else:
            parse_contract_text(text, name)
    except ContractError as exc:
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
