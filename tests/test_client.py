from __future__ import annotations

from typing import Optional

import httpx
import pytest

from sitewarm import (
    AsyncSiteWarm,
    SiteWarm,
    SiteWarmAuthenticationError,
    SiteWarmConnectionError,
    SiteWarmError,
    SiteWarmNotFoundError,
    SiteWarmRateLimitError,
    SiteWarmServerError,
    SiteWarmValidationError,
    SiteWarmWaitTimeoutError,
)
from sitewarm._retry import backoff_delay, parse_retry_after

KEY = "swk_secret_key"
BASE = "https://api.example.test/v1"


def make(handler, **kw) -> SiteWarm:
    kw.setdefault("retry_base_delay", 0.0)
    return SiteWarm(KEY, BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler)), **kw)


def test_request_shape_and_auth_header():
    seen: list[httpx.Request] = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"id": "s1"})

    assert make(h).sites.get("a/b") == {"id": "s1"}
    r = seen[0]
    assert r.method == "GET" and r.url.raw_path == b"/v1/sites/a%2Fb"
    assert r.headers["authorization"] == f"Bearer {KEY}"


def test_query_string_skips_none_and_lowercases_bools():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(200, json={"data": [], "page": {"has_more": False, "next_cursor": None}})

    make(h).sites.list(limit=5, cursor=None, verified=True)
    assert dict(seen[0].url.params) == {"limit": "5", "verified": "true"}


@pytest.mark.parametrize(
    "status,cls",
    [(401, SiteWarmAuthenticationError), (404, SiteWarmNotFoundError), (422, SiteWarmValidationError),
     (429, SiteWarmRateLimitError), (500, SiteWarmServerError)],
)
def test_problem_to_typed_error(status, cls):
    def h(req):
        return httpx.Response(
            status,
            json={"type": "https://x.test/problems/some-slug", "title": "T", "detail": "D", "code": "some-code"},
            headers={"content-type": "application/problem+json", "Retry-After": "0", "RateLimit-Remaining": "0"},
        )

    with pytest.raises(cls) as e:
        make(h, max_retries=0).sites.get("s1")
    assert e.value.status == status and e.value.code == "some-code" and e.value.rate_limit == {"remaining": 0}
    assert KEY not in str(e.value) and KEY not in repr(e.value)


def test_retries_429_then_succeeds_and_honors_retry_after():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(429, headers={"Retry-After": "0"}) if n["c"] < 3 else httpx.Response(200, json={})

    make(h).sites.get("s1")
    assert n["c"] == 3


def test_retry_after_longer_than_cap_is_surfaced():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        return httpx.Response(429, headers={"Retry-After": "120"})

    with pytest.raises(SiteWarmRateLimitError) as e:
        make(h, max_retry_delay=5).sites.get("s1")
    assert n["c"] == 1 and e.value.retry_after_seconds == 120


def test_post_without_key_is_not_retried_on_5xx_but_keyed_post_is(monkeypatch):
    from sitewarm._generated.operations import OPERATIONS, Operation

    # Every POST of the current spec takes a key, so exercise the no-key path with a synthetic operation.
    monkeypatch.setitem(OPERATIONS, "fakePost", Operation("POST", "/fake", "none"))
    calls = []

    def h(req):
        calls.append(req)
        return httpx.Response(503)

    with pytest.raises(SiteWarmServerError):
        make(h, max_retries=2).call("fakePost")
    assert len(calls) == 1 and "idempotency-key" not in calls[0].headers

    calls.clear()
    with pytest.raises(SiteWarmServerError):
        make(h, max_retries=2).warm_runs.create("s1", {"source": {"type": "sitemap"}})
    assert len(calls) == 3
    keys = {c.headers["idempotency-key"] for c in calls}
    assert len(keys) == 1  # one key for the call, reused by every retry


def test_given_idempotency_key_is_sent():
    seen = []

    def h(req):
        seen.append(req)
        return httpx.Response(202, json={"id": "run1"})

    make(h).warm_runs.create("s1", {"x": 1}, idempotency_key="mine")
    assert seen[0].headers["idempotency-key"] == "mine"


def test_network_error_is_wrapped_without_secrets():
    def h(req):
        raise httpx.ConnectError(f"boom {KEY}")

    with pytest.raises(SiteWarmConnectionError) as e:
        make(h, max_retries=1).sites.get("s1")
    assert KEY not in str(e.value)


def test_paginate_follows_cursor_and_detects_stall():
    pages = {
        None: {"data": [1, 2], "page": {"has_more": True, "next_cursor": "c1"}},
        "c1": {"data": [3], "page": {"has_more": False, "next_cursor": None}},
    }

    def h(req):
        return httpx.Response(200, json=pages[req.url.params.get("cursor")])

    assert list(make(h).sites.list_all()) == [1, 2, 3]

    def stuck(req):
        return httpx.Response(200, json={"data": [1], "page": {"has_more": True, "next_cursor": "c1"}})

    with pytest.raises(SiteWarmError, match="did not advance"):
        list(make(stuck).sites.list_all())


def test_wait_polls_until_terminal_and_times_out_without_canceling():
    seq = iter(["queued", "running", "completed"])

    def h(req):
        return httpx.Response(200, json={"status": next(seq)}, headers={"Retry-After": "0"})

    assert make(h).warm_runs.wait("r1", min_poll_interval=0)["status"] == "completed"

    methods = []

    def never(req):
        methods.append(req.method)
        return httpx.Response(200, json={"status": "running"})

    with pytest.raises(SiteWarmWaitTimeoutError):
        make(never).warm_runs.wait("r1", timeout=0.05, poll_interval=0.01, min_poll_interval=0.01)
    assert set(methods) == {"GET"}


def test_pydantic_body_is_serialized():
    from pydantic import BaseModel

    class B(BaseModel):
        a: int
        b: Optional[int] = None

    seen = []

    def h(req):
        seen.append(req.content)
        return httpx.Response(200, json={})

    make(h).sites.create(B(a=1))
    assert seen[0] == b'{"a": 1}'


@pytest.mark.parametrize(
    "url", ["http://api.example.test/v1", "https://u:p@api.example.test", "https://x.test/v1?a=1", "api.example.test", ""],
)
def test_base_url_validation(url):
    with pytest.raises(SiteWarmError):
        SiteWarm(KEY, url)


def test_localhost_http_allowed_and_key_validation():
    SiteWarm(KEY, "http://localhost:8080/v1")
    for bad in ("", "has space", "tab\t"):
        with pytest.raises(SiteWarmError) as e:
            SiteWarm(bad, BASE)
        assert bad not in str(e.value) or bad == ""


def test_repr_hides_key():
    assert KEY not in repr(SiteWarm(KEY, BASE))


def test_unknown_operation_and_missing_path_param():
    c = make(lambda r: httpx.Response(200, json={}))
    with pytest.raises(SiteWarmError, match="unknown operation"):
        c.call("nope")
    with pytest.raises(SiteWarmError, match="missing path parameter"):
        c.call("getSite")


def test_retry_helpers():
    assert parse_retry_after("7") == 7
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:10 GMT", now=1445412480 - 5 + 0) is not None
    assert backoff_delay(10, 1.0, 4.0, rand=lambda: 0.0) == 2.0
    assert backoff_delay(10, 1.0, 4.0, rand=lambda: 1.0) == 4.0


async def test_async_client_retries_and_paginates():
    n = {"c": 0}

    def h(req):
        n["c"] += 1
        if n["c"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"data": [1], "page": {"has_more": False, "next_cursor": None}})

    async with AsyncSiteWarm(KEY, BASE, retry_base_delay=0.0,
                             http_client=httpx.AsyncClient(transport=httpx.MockTransport(h))) as c:
        items = [i async for i in c.paginate("listSites")]
    assert items == [1] and n["c"] == 2
