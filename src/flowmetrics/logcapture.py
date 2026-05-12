"""Capture stderr + logging + warnings into an in-memory buffer.

Used by JSON-mode output so an agent that consumes only stdout still
sees every diagnostic that would otherwise go to stderr.
"""

from __future__ import annotations

import io
import logging
import sys
import warnings
from types import TracebackType
from typing import Self, TextIO


class LogCapture:
    def __init__(self) -> None:
        self._lines: list[str] = []
        self._buffer: io.StringIO | None = None
        self._original_stderr: TextIO | None = None
        self._original_showwarning = None
        self._handler: logging.Handler | None = None

    @property
    def lines(self) -> list[str]:
        return self._lines

    def __enter__(self) -> Self:
        self._buffer = io.StringIO()
        self._original_stderr = sys.stderr
        sys.stderr = self._buffer

        self._handler = logging.StreamHandler(self._buffer)
        self._handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(self._handler)

        self._original_showwarning = warnings.showwarning

        def _route(message, category, filename, lineno, file=None, line=None):
            text = warnings.formatwarning(message, category, filename, lineno, line)
            assert self._buffer is not None
            self._buffer.write(text)

        warnings.showwarning = _route  # ty: ignore[invalid-assignment]
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._buffer is not None:
            captured = self._buffer.getvalue()
            self._lines = [line for line in captured.splitlines() if line.strip()]

        if self._original_stderr is not None:
            sys.stderr = self._original_stderr

        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)

        if self._original_showwarning is not None:
            warnings.showwarning = self._original_showwarning  # ty: ignore[invalid-assignment]
