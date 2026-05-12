from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


class CacheMiss(Exception):
    """Raised when a read-only cache has no entry for a key."""


class FileCache:
    """Disk-backed JSON cache keyed by sha256(query+variables).

    Existence-of-file = cache hit. No expiry; reruns over the same
    (query, variables) make zero network calls.
    """

    def __init__(self, directory: Path | str):
        self.directory = Path(directory)

    @staticmethod
    def make_key(query: str, variables: dict[str, Any]) -> str:
        payload = json.dumps({"q": query, "v": variables}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            return None
        return json.loads(path.read_text())

    def put(self, key: str, value: dict[str, Any]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(json.dumps(value, sort_keys=True, indent=2))
