"""Behavioural spec for LogCapture.

LogCapture exists so JSON-mode output never silently drops stderr,
logging, or warnings. The contract:

1. Inside the `with` block, writes to sys.stderr are redirected to an
   internal buffer (not the real terminal).
2. `logging` calls at WARNING and above land in the same buffer.
3. `warnings.warn(...)` lands in the buffer.
4. After exit, sys.stderr, the logging handlers, and warnings.showwarning
   are restored to their previous values.
5. `cap.lines` is empty during the block, non-empty after exit if
   anything was captured, and excludes blank lines.
6. Restoration still happens if an exception is raised inside the block.
"""

from __future__ import annotations

import logging
import sys
import warnings

import pytest

from flowmetrics.logcapture import LogCapture


class TestStderrCapture:
    def test_stderr_writes_inside_block_go_to_buffer(self, capsys):
        with LogCapture() as cap:
            print("hello on stderr", file=sys.stderr)
            # During capture, lines are empty (we expose them on exit)
            assert cap.lines == []
        assert any("hello on stderr" in line for line in cap.lines)
        # Real stderr should NOT have received it
        captured = capsys.readouterr()
        assert "hello on stderr" not in captured.err

    def test_stderr_restored_after_exit(self):
        original = sys.stderr
        with LogCapture():
            assert sys.stderr is not original
        assert sys.stderr is original

    def test_stderr_restored_on_exception(self):
        original = sys.stderr
        with pytest.raises(RuntimeError, match="boom"), LogCapture():
            print("partial", file=sys.stderr)
            raise RuntimeError("boom")
        assert sys.stderr is original


class TestLoggingCapture:
    def test_warning_log_lands_in_buffer(self):
        logger = logging.getLogger("flowmetrics.test")
        with LogCapture() as cap:
            logger.warning("something is off")
        assert any("something is off" in line for line in cap.lines)

    def test_handler_removed_after_exit(self):
        root = logging.getLogger()
        initial_handlers = list(root.handlers)
        with LogCapture():
            assert len(root.handlers) == len(initial_handlers) + 1
        assert root.handlers == initial_handlers


class TestWarningsCapture:
    def test_warn_lands_in_buffer(self):
        with LogCapture() as cap:
            warnings.warn("deprecated thing", DeprecationWarning, stacklevel=1)
        assert any("deprecated thing" in line for line in cap.lines)

    def test_showwarning_restored(self):
        original = warnings.showwarning
        with LogCapture():
            pass
        assert warnings.showwarning is original


class TestLinesShape:
    def test_blank_lines_are_dropped(self):
        with LogCapture() as cap:
            print("first\n\n\nsecond", file=sys.stderr)
        assert "" not in cap.lines
        assert any("first" in line for line in cap.lines)
        assert any("second" in line for line in cap.lines)

    def test_empty_capture_yields_empty_list(self):
        with LogCapture() as cap:
            pass
        assert cap.lines == []
