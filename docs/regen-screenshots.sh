#!/usr/bin/env bash
# Regenerate every screenshot in docs/screenshots/ from a running
# `flow serve` instance.
#
# Prerequisites:
#   1. Workflows materialised:
#        flow materialise astral-uv-week --workflows-dir contracts --data-dir data
#        flow materialise apache-cassandra-week --workflows-dir contracts --data-dir data
#      The starter YAMLs live in samples/ — copy what you need into
#      contracts/ first.
#
#   2. Server up on port 8000 (default):
#        flow serve --workflows-dir contracts --data-dir data
#
#   3. Playwright installed (already a dev dep):
#        uv pip install playwright
#        uv run playwright install chromium

set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$REPO/docs/screenshots"
PORT="${PORT:-8000}"
GH="${GITHUB_WORKFLOW:-astral-uv-week}"
JIRA="${JIRA_WORKFLOW:-apache-cassandra-week}"

mkdir -p "$OUT"

uv run python - <<PY
from playwright.sync_api import sync_playwright

out = "$OUT"
port = "$PORT"
gh = "$GH"
jira = "$JIRA"
base = f"http://127.0.0.1:{port}"

with sync_playwright() as p:
    b = p.chromium.launch()
    pg = b.new_page(viewport={"width":1400,"height":1100})

    pg.goto(f"{base}/", timeout=15000)
    pg.wait_for_selector("h1", timeout=5000); pg.wait_for_timeout(800)
    pg.screenshot(path=f"{out}/home.png", full_page=False, clip={"x":0,"y":0,"width":1400,"height":500})

    for slug, prefix in ((gh, "github"), (jira, "jira")):
        pg.goto(f"{base}/workflows/{slug}/", timeout=30000)
        pg.wait_for_load_state("networkidle", timeout=20000); pg.wait_for_timeout(4500)
        pg.screenshot(path=f"{out}/{prefix}-dashboard.png", full_page=True)

    for slug, prefix, metric in (
        (gh, "github", "aging"),
        (gh, "github", "throughput"),
        (jira, "jira", "cfd"),
    ):
        pg.goto(f"{base}/workflows/{slug}/metrics/{metric}", timeout=30000)
        pg.wait_for_load_state("networkidle", timeout=15000); pg.wait_for_timeout(3500)
        pg.screenshot(path=f"{out}/{prefix}-{metric}-detail.png", full_page=False,
                      clip={"x":0,"y":0,"width":1400,"height":900})

    pg.goto(f"{base}/admin/contracts/new", timeout=15000)
    pg.wait_for_selector("input[name='name']", timeout=5000); pg.wait_for_timeout(1000)
    pg.screenshot(path=f"{out}/contract-wizard.png", full_page=True)

    b.close()
    print("wrote", out)
PY
