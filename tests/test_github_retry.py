"""Behavioural spec for GitHubClient retry logic.

GitHub's GraphQL endpoint occasionally returns transient 5xx (real
example: huggingface/transformers returned 504 during a 30-day fetch).
We retry transient failures up to `max_retries` times with exponential
backoff. Persistent failures still propagate.

Contract:
1. Successful request → no retry needed.
2. 504 / 503 / 502 / 500 followed by success → returns the success.
3. All attempts fail → raises after `max_retries`.
4. 4xx (auth / bad request) → no retry (fails fast).
5. Cache hits are never retried (no network involved).
"""

from __future__ import annotations

import httpx
import pytest

from flowmetrics.cache import FileCache
from flowmetrics.github import GitHubClient


def _build_client(tmp_path, transport: httpx.MockTransport, token: str = "test-token"):
    return GitHubClient(
        FileCache(tmp_path),
        token=token,
        http_client=httpx.Client(transport=transport),
        max_retries=3,
        retry_initial_seconds=0.0,  # no real sleeping in tests
    )


def _ok_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": {"viewer": {"login": "octocat"}}, "rateLimit": {}},
    )


class TestSuccessNoRetry:
    def test_single_successful_call(self, tmp_path):
        calls = []

        def handler(request):
            calls.append(request)
            return _ok_response(request)

        client = _build_client(tmp_path, httpx.MockTransport(handler))
        payload = client.graphql("query{viewer{login}}", {})
        assert payload["data"]["viewer"]["login"] == "octocat"
        assert len(calls) == 1


class TestTransientRetry:
    def test_504_then_success_retries_and_succeeds(self, tmp_path):
        responses = iter(
            [
                httpx.Response(504, text="Gateway Timeout"),
                httpx.Response(503, text="Service Unavailable"),
                _ok_response(None),
            ]
        )
        calls = []

        def handler(request):
            calls.append(request)
            return next(responses)

        client = _build_client(tmp_path, httpx.MockTransport(handler))
        payload = client.graphql("query{viewer{login}}", {})
        assert payload["data"]["viewer"]["login"] == "octocat"
        assert len(calls) == 3  # two failed, third succeeded

    def test_500_502_503_504_all_retried(self, tmp_path):
        for code in (500, 502, 503, 504):
            responses = iter([httpx.Response(code, text="Transient"), _ok_response(None)])

            def handler(request, _r=responses):
                return next(_r)

            client = _build_client(tmp_path / f"d{code}", httpx.MockTransport(handler))
            payload = client.graphql(f"query{{viewer{{login{code}}}}}", {})
            assert "data" in payload


class TestFailFast:
    def test_404_does_not_retry(self, tmp_path):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(404, text="Not Found")

        client = _build_client(tmp_path, httpx.MockTransport(handler))
        with pytest.raises(httpx.HTTPStatusError):
            client.graphql("query{viewer{login}}", {})
        assert len(calls) == 1  # no retry

    def test_401_does_not_retry(self, tmp_path):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(401, text="Unauthorized")

        client = _build_client(tmp_path, httpx.MockTransport(handler))
        with pytest.raises(httpx.HTTPStatusError):
            client.graphql("query{viewer{login}}", {})
        assert len(calls) == 1


class TestExhaustion:
    def test_all_retries_fail_propagates(self, tmp_path):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(504, text="always down")

        client = _build_client(tmp_path, httpx.MockTransport(handler))
        with pytest.raises(httpx.HTTPStatusError):
            client.graphql("query{viewer{login}}", {})
        assert len(calls) == 4  # 1 initial + 3 retries (max_retries=3)


class TestCacheBypassesRetry:
    def test_cache_hit_never_calls_network(self, tmp_path):
        cache = FileCache(tmp_path)
        cache.put(FileCache.make_key("q", {}), {"data": "cached"})

        # Transport that would raise on any call
        def handler(request):
            raise AssertionError("should not have been called")

        client = GitHubClient(
            cache,
            token="x",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
            max_retries=3,
            retry_initial_seconds=0.0,
        )
        assert client.graphql("q", {}) == {"data": "cached"}
