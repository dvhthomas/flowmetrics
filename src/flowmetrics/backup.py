"""Warehouse backup / restore.

A backup is a single `.tar.gz` carrying:

  - every file under `data_dir` except (by default) the source-API
    cache, which is regenerable;
  - optionally `<workflows-dir>/contracts.db` (the config DB),
    snapshotted via SQLite's online backup API so a live writer
    can't tear the file mid-copy. Lands inside the tarball at
    `_config/contracts.db`;
  - a `flowmetrics-backup.json` header recording schema version,
    flowmetrics version, DuckDB version, and a SHA-256 of every
    payload file.

Restore verifies the header + every checksum BEFORE writing anything
to the target directory. A corrupted or tampered backup fails before
it can damage a half-restored warehouse. Targets must be empty
unless `--force` is given.

Restore is selectable: data-only, config-only, or both (default). The
single tarball is the unit of backup; the unit of restore is whichever
subset the operator asks for.

The format is intentionally stdlib-only (tarfile + gzip + sqlite3):
no external compressor, no boto3, no zstandard. S3 / cloud targets
live behind an optional dep and are out of scope here.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Sub-directories under `data_dir` whose contents are re-fetchable
# from the source API and so excluded from backups by default.
_CACHE_DIR_NAMES = frozenset({".cache", "cache"})

# Path inside the tarball that holds the integrity header.
_HEADER_NAME = "flowmetrics-backup.json"

# Prefix inside the tarball for config-DB payload. The `_` mirrors
# the existing `_backups/` + `_status/` convention and guarantees
# no collision with a real warehouse subdirectory.
_CONFIG_PREFIX = "_config/"
_CONFIG_DB_RELPATH = f"{_CONFIG_PREFIX}workflows.db"
# Legacy backups (made before the rename) carry the DB at
# `_config/contracts.db`. Restore extracts that filename as-is;
# the next WorkflowStore init auto-renames it to workflows.db.

# Header schema URI — bumped on any breaking change to the layout.
SCHEMA_URI = "flowmetrics.backup.v1"


def _flowmetrics_version() -> str:
    """Best-effort package version. `importlib.metadata` returns the
    installed dist version; a source checkout without an install
    falls back to `unknown`."""
    try:
        from importlib.metadata import version
        return version("flowmetrics")
    except Exception:
        return "unknown"


def _duckdb_version() -> str:
    try:
        import duckdb
        return duckdb.__version__
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class BackupHeader:
    schema: str
    flowmetrics_version: str
    duckdb_version: str
    created_at: str          # ISO-8601 UTC
    files: dict[str, str]    # relative path → sha256 hex

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "schema": self.schema,
                "flowmetrics_version": self.flowmetrics_version,
                "duckdb_version": self.duckdb_version,
                "created_at": self.created_at,
                "files": self.files,
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> BackupHeader:
        d = json.loads(raw)
        return cls(
            schema=d["schema"],
            flowmetrics_version=d["flowmetrics_version"],
            duckdb_version=d["duckdb_version"],
            created_at=d["created_at"],
            files=d["files"],
        )


def _should_skip(rel: Path, include_cache: bool) -> bool:
    """Skip the cache subtree (by default) and anything inside an
    obvious meta-directory we created (`_backups/` from a previous
    run, `_status/` from the daily manifest)."""
    parts = rel.parts
    if not include_cache and any(p in _CACHE_DIR_NAMES for p in parts):
        return True
    return bool(parts and parts[0] == "_backups")


def _enumerate_payload(data_dir: Path, include_cache: bool) -> list[Path]:
    """All files under `data_dir` that belong in the tarball. Sorted
    for deterministic header ordering (so two backups of the same
    state produce identical archives)."""
    out: list[Path] = []
    for p in sorted(data_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(data_dir)
        if _should_skip(rel, include_cache):
            continue
        out.append(p)
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _snapshot_sqlite(src: Path) -> bytes:
    """Return a consistent snapshot of a SQLite DB as bytes via the
    online backup API. Safe with a concurrent writer holding an open
    connection (the API serialises pages through the SQLite engine,
    not the filesystem)."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td) / "snapshot.db"
        src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        try:
            dst_con = sqlite3.connect(str(tmp))
            try:
                src_con.backup(dst_con)
            finally:
                dst_con.close()
        finally:
            src_con.close()
        return tmp.read_bytes()


def _collect_config_payload(contracts_dir: Path | None) -> dict[str, bytes]:
    """Snapshot any config DBs under `contracts_dir` worth including.
    Today: just `workflows.db`. Returns {tarball-relpath: bytes}.

    Performs the one-time legacy filename rename inline (rather than
    constructing WorkflowStore, which would open a connection and
    fight with a live writer). `_resolve_db_path` is the canonical
    rename helper and does not open the DB.
    """
    if contracts_dir is None:
        return {}
    from .workflows_db import _resolve_db_path
    # Idempotent file-system rename; safe even if a concurrent server
    # has the new file open (we don't touch the DB itself here).
    contracts_dir.mkdir(parents=True, exist_ok=True)
    src = _resolve_db_path(contracts_dir)
    out: dict[str, bytes] = {}
    if src.exists():
        out[_CONFIG_DB_RELPATH] = _snapshot_sqlite(src)
    return out


def write_backup(
    data_dir: Path,
    output: Path,
    *,
    include_cache: bool = False,
    contracts_dir: Path | None = None,
) -> BackupHeader:
    """Write a `.tar.gz` backup of `data_dir` (and optionally
    `<contracts_dir>/contracts.db`) to `output`. Returns the header
    that was embedded.

    When `contracts_dir` is given AND it contains a `contracts.db`,
    a consistent snapshot (taken via SQLite's online backup API) is
    placed inside the tarball at `_config/contracts.db` with a
    SHA-256 in the header. Callers that don't pass `contracts_dir`
    get the same data-only archive as before.
    """
    payload = _enumerate_payload(data_dir, include_cache)
    config_bytes = _collect_config_payload(contracts_dir)
    # tar standard mandates `/` separators; Path's `str()` returns
    # `\` on Windows, which produced a header/arcname mismatch where
    # `tar.getmember()` couldn't locate files the header claimed
    # existed. `as_posix()` pins the on-tape layout cross-platform.
    files: dict[str, str] = {
        p.relative_to(data_dir).as_posix(): _sha256(p) for p in payload
    }
    for relpath, raw in config_bytes.items():
        files[relpath] = hashlib.sha256(raw).hexdigest()
    header = BackupHeader(
        schema=SCHEMA_URI,
        flowmetrics_version=_flowmetrics_version(),
        duckdb_version=_duckdb_version(),
        created_at=datetime.now(UTC).isoformat(),
        files=files,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as tar:
        # Header first so a streaming restore can fail fast on schema
        # mismatch before reading any payload.
        header_bytes = header.to_json()
        info = tarfile.TarInfo(name=_HEADER_NAME)
        info.size = len(header_bytes)
        tar.addfile(info, io.BytesIO(header_bytes))
        for p in payload:
            tar.add(p, arcname=p.relative_to(data_dir).as_posix())
        for relpath, raw in config_bytes.items():
            info = tarfile.TarInfo(name=relpath)
            info.size = len(raw)
            tar.addfile(info, io.BytesIO(raw))
    return header


class BackupError(Exception):
    """Raised when a backup is malformed, corrupted, or tampered."""


def read_header(input_path: Path) -> BackupHeader:
    """Open the tarball, read just the header, close. Used by restore
    to fail fast before allocating restore-target paths."""
    try:
        with tarfile.open(input_path, "r:gz") as tar:
            try:
                member = tar.getmember(_HEADER_NAME)
            except KeyError as exc:
                raise BackupError(
                    f"no {_HEADER_NAME} inside {input_path} — not a "
                    "flowmetrics backup."
                ) from exc
            f = tar.extractfile(member)
            if f is None:
                raise BackupError(
                    f"could not read {_HEADER_NAME} from {input_path}."
                )
            raw = f.read()
    except (tarfile.ReadError, EOFError, OSError) as exc:
        raise BackupError(
            f"{input_path} is not a readable .tar.gz: {exc}"
        ) from exc
    try:
        return BackupHeader.from_bytes(raw)
    except (KeyError, json.JSONDecodeError) as exc:
        raise BackupError(
            f"{_HEADER_NAME} inside {input_path} is malformed: {exc}"
        ) from exc


def _is_target_dirty(target: Path) -> bool:
    if not target.exists():
        return False
    if not target.is_dir():
        # A file at the target path counts as "in the way".
        return True
    return any(target.iterdir())


def _split_files(
    files: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Partition a header's file list into (data_files, config_files).
    Anything under `_config/` is config; everything else is data."""
    data: dict[str, str] = {}
    config: dict[str, str] = {}
    for relpath, digest in files.items():
        if relpath.startswith(_CONFIG_PREFIX):
            config[relpath] = digest
        else:
            data[relpath] = digest
    return data, config


def restore_backup(
    input_path: Path,
    data_dir: Path,
    *,
    force: bool = False,
    contracts_dir: Path | None = None,
    restore_data: bool = True,
    restore_config: bool = True,
) -> BackupHeader:
    """Verify + extract `input_path`.

    Scope (data-vs-config) is controlled by `restore_data` /
    `restore_config`; both default to True (= the previous behaviour
    when the tarball was data-only). Data files extract under
    `data_dir`; config files extract under `contracts_dir`.

    Bails before writing anything when:
      - The tarball isn't a valid gzipped tar.
      - The header is missing or malformed.
      - The schema is from a newer version we don't understand.
      - Any payload file's SHA-256 doesn't match the header.
      - A scoped-in target exists and is non-empty AND `force` is False.
      - `restore_config=True` but the tarball carries no config payload
        (the operator asked for something the backup doesn't have).
      - `restore_config=True` but no `contracts_dir` was supplied.
    """
    if not restore_data and not restore_config:
        raise BackupError(
            "nothing to restore — both data and config were disabled."
        )

    header = read_header(input_path)
    if header.schema != SCHEMA_URI:
        raise BackupError(
            f"unknown backup schema {header.schema!r}; this build "
            f"understands {SCHEMA_URI!r}."
        )

    data_files, config_files = _split_files(header.files)

    # An EXPLICIT config-only restore against a data-only backup
    # is an operator error — they asked for something the tarball
    # can't deliver. The default (both = True, but no config in
    # the archive) is fine; we just silently skip the config step.
    if restore_config and not restore_data and not config_files:
        raise BackupError(
            f"{input_path} carries no config payload "
            f"(no `{_CONFIG_PREFIX}…` entries). Run a fresh `flow "
            f"backup --workflows-dir …` and try again, or drop "
            f"`--config-only`."
        )

    # contracts_dir is only required if we're actually going to
    # write config — i.e. the user wants config AND there's config
    # in the archive.
    will_extract_config = (
        restore_config and bool(config_files) and contracts_dir is not None
    )
    if restore_config and config_files and contracts_dir is None:
        raise BackupError(
            "restoring config requires --workflows-dir to know where "
            "to write contracts.db."
        )

    # Dirty-check only the targets we're about to touch. Restoring
    # data-only doesn't care that contracts_dir is non-empty, and
    # vice versa.
    if (
        restore_data and data_files
        and _is_target_dirty(data_dir) and not force
    ):
        raise BackupError(
            f"target {data_dir} is non-empty. Pass --force to "
            f"overwrite, or pick a fresh directory."
        )
    if (
        will_extract_config
        and _is_target_dirty(contracts_dir)  # type: ignore[arg-type]
        and not force
    ):
        raise BackupError(
            f"target {contracts_dir} is non-empty. Pass --force to "
            f"overwrite, or pick a fresh directory."
        )

    # Verify EVERY payload file in-memory (regardless of scope)
    # before touching disk. Hash-checking the whole archive on
    # restore — even files we won't extract — is cheap and catches
    # corruption in the unscoped half so an operator who later
    # re-restores with `--config-only` already knows the tarball is
    # bad.
    try:
        with tarfile.open(input_path, "r:gz") as tar:
            for relpath, expected in header.files.items():
                try:
                    member = tar.getmember(relpath)
                except KeyError as exc:
                    raise BackupError(
                        f"backup is missing {relpath!r} listed in the header."
                    ) from exc
                stream = tar.extractfile(member)
                if stream is None:
                    raise BackupError(
                        f"could not read {relpath!r} from the backup."
                    )
                h = hashlib.sha256()
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                if h.hexdigest() != expected:
                    raise BackupError(
                        f"checksum mismatch for {relpath!r} — backup is "
                        "corrupted or tampered with."
                    )
    except (tarfile.ReadError, EOFError, OSError) as exc:
        raise BackupError(f"could not read {input_path}: {exc}") from exc

    # Now we can extract — every byte will match the header.
    if restore_data and data_files:
        data_dir.mkdir(parents=True, exist_ok=True)
    if will_extract_config:
        contracts_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]

    with tarfile.open(input_path, "r:gz") as tar:
        if restore_data:
            for relpath in data_files:
                member = tar.getmember(relpath)
                target = data_dir / relpath
                target.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(member)
                assert f is not None
                target.write_bytes(f.read())
        if will_extract_config:
            for relpath in config_files:
                member = tar.getmember(relpath)
                # Strip the `_config/` prefix so `_config/contracts.db`
                # lands at `<contracts_dir>/contracts.db`.
                rel_inside = relpath[len(_CONFIG_PREFIX):]
                target = contracts_dir / rel_inside  # type: ignore[operator]
                target.parent.mkdir(parents=True, exist_ok=True)
                f = tar.extractfile(member)
                assert f is not None
                target.write_bytes(f.read())
    return header
