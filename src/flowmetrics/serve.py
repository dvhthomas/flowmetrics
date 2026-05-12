"""Tiny local server for browsing the samples/ directory.

Registered as `samples` in pyproject.toml's [project.scripts]:

    uv run samples

Optional flags:
    --port 8765    Port to bind (default 8765).
    --no-open      Skip auto-opening the browser.
    --dir PATH     Directory to serve (default: ./samples relative to cwd).

Localhost-only by design — don't expose this beyond your machine.
"""

from __future__ import annotations

import argparse
import errno
import http.server
import socketserver
import sys
import webbrowser
from functools import partial
from pathlib import Path


class _ReuseTCPServer(socketserver.TCPServer):
    """Set SO_REUSEADDR so quick Ctrl-C → restart doesn't trip TIME_WAIT."""

    allow_reuse_address = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--dir", type=Path, default=Path("samples"))
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)

    directory = args.dir.resolve()
    if not directory.is_dir():
        sys.stderr.write(
            f"error: {directory} is not a directory.\n"
            "  Run `uv run python scripts/generate_samples.py` first.\n"
        )
        return 1

    handler = partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    url = f"http://127.0.0.1:{args.port}/"

    try:
        httpd = _ReuseTCPServer(("127.0.0.1", args.port), handler)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            sys.stderr.write(
                f"error: port {args.port} is already in use.\n"
                "  Another instance may already be running. Options:\n"
                f"    - find it:  lsof -ti:{args.port}\n"
                f"    - kill it:  kill $(lsof -ti:{args.port})\n"
                f"    - try a different port:  uv run samples --port {args.port + 1}\n"
            )
            return 1
        raise

    # Loopback only — the samples include real PR titles from public repos
    # but a server on 0.0.0.0 is still a needless attack surface.
    with httpd:
        print(f"Serving {directory} at {url}")
        print("Press Ctrl-C to stop.")
        if not args.no_open:
            webbrowser.open(url + "index.html")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
