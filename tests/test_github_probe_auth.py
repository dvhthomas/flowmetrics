"""The builder's GitHub probe + dry-run calls must use the same
auth token the rest of the app uses — otherwise they hit GitHub's
60/hour anonymous rate limit and labels silently fail to load.
"""

from __future__ import annotations

from flowmetrics import source_probe as sp


class _FakeResp:
    def __init__(self, status_code=200, json_body=None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else []
        self.headers = {"content-type": "application/json"}

    def json(self):
        return self._json


def test_github_helper_adds_auth_header_when_token_available(monkeypatch):
    captured = {}

    def fake_get(url, timeout=None, headers=None):
        captured["url"] = url
        captured["headers"] = headers or {}
        return _FakeResp(200, [{"name": "bug"}])

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(
        "flowmetrics.sources.github.resolve_token", lambda: "tok-123"
    )

    out = sp.probe_source_vocab(
        "github", {"repo": "owner/repo"}
    )
    # The repo labels came back…
    assert any(label["name"] == "bug" for label in out["labels"])
    # …and the request carried the bearer token.
    assert captured["headers"].get("Authorization") == "Bearer tok-123"


def test_github_helper_degrades_to_anonymous_without_token(monkeypatch):
    """No token configured → still works (anonymous), just no auth
    header. Better a rate-limited probe than a crash."""
    captured = {}

    def fake_get(url, timeout=None, headers=None):
        captured["headers"] = headers or {}
        return _FakeResp(200, [])

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    def _boom():
        raise RuntimeError("no token")

    monkeypatch.setattr(
        "flowmetrics.sources.github.resolve_token", _boom
    )

    out = sp.probe_source_vocab(
        "github", {"repo": "owner/repo"}
    )
    assert "labels" in out  # didn't crash
    assert "Authorization" not in captured["headers"]


def test_dry_run_github_fetch_uses_auth(monkeypatch):
    captured = {}

    def fake_get(url, timeout=None, headers=None):
        captured["headers"] = headers or {}
        return _FakeResp(200, {"items": []})

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(
        "flowmetrics.sources.github.resolve_token", lambda: "tok-xyz"
    )

    sp.dry_run_fetch(
        source="github", target={"repo": "owner/repo"},
        since="2026-04-01", items_cap=10,
    )
    assert captured["headers"].get("Authorization") == "Bearer tok-xyz"
